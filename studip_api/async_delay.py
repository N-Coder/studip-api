import asyncio
import logging
from asyncio import Task
from typing import Callable, Coroutine

import attr
from attr import Factory

log = logging.getLogger("studip_api.async_delay")


async def await_idle(delay=0, max_badness=5, sleep_time=10):
    await asyncio.sleep(delay)

    wait_loop_counter = 0
    while True:
        badness = get_current_badness(sleep_time, wait_loop_counter)
        if badness > max_badness:
            log.debug("Current badness %s > %s after waiting for %s loops, deferring",
                      badness, max_badness, wait_loop_counter)
            wait_loop_counter += 1
            await asyncio.sleep(sleep_time)
        else:
            log.debug("Current badness %s <= %s after waiting for %s loops, running",
                      badness, max_badness, wait_loop_counter)
            break


# noinspection PyProtectedMember
def get_current_badness(sleep_time, wait_loop_counter, loop=None):
    if not loop:
        loop = asyncio.get_event_loop()

    # acquired_cons = len(self.ahttp.connector._acquired)
    pending_tasks = len(loop._ready)
    if loop._scheduled:
        timeout = loop._scheduled[0]._when - loop.time()
    else:
        timeout = sleep_time  # expect no work for the next few seconds

    badness = pending_tasks - 0.25 * timeout - 0.5 * wait_loop_counter
    log.debug("Badness %s: pending_tasks %s, timeout %s, wait_loop_counter %s",
              badness, pending_tasks, timeout, wait_loop_counter)
    return badness


def execute_sequentially(*coro_funcs):
    async def coro():
        for coro_func in coro_funcs:
            await coro_func()

    return asyncio.ensure_future(coro())


@attr.s()
class DelayLatch(object):
    sleep_fun = attr.ib(default=asyncio.sleep)  # type: Callable[[int],Coroutine]
    time_fun = attr.ib(default=lambda: asyncio.get_event_loop().time())  # type: Callable[[],int]

    _task = attr.ib(init=False, default=None)  # type: Task
    _run_at = attr.ib(init=False, default=-1)  # type: int
    _cancelled = attr.ib(init=False, default=False)  # type: bool

    async def wait_on_latch(self):
        if not self.is_open():
            await self._task

    def reopen_at(self, timestamp):
        self.__set_reopen_at(max(self._run_at, timestamp))

    def reopen_in(self, delay):
        self.__set_reopen_at(max(self._run_at, self.time_fun() + delay))

    def reopen_now(self):
        self.__set_reopen_at(self.time_fun() - 1)

    def __set_reopen_at(self, run_at):
        self._cancelled = False
        self._run_at = run_at
        if self.is_open():
            self._task = asyncio.ensure_future(self.__delayed_reopen_task())  # start new task
        else:
            self._task.cancel()  # interrupt task

    def cancel_latch(self):
        self._cancelled = True
        if not self.is_open():
            self._task.cancel()  # cancel task

    def is_open(self):
        return not self._task or self._task.done()

    def was_cancelled(self):
        return self._task and self._task.cancelled()

    async def __delayed_reopen_task(self):
        while self.time_fun() < self._run_at:
            assert self._task == Task.current_task(), "%s: %s != %s" % (self, self._task, Task.current_task())
            delta = self._run_at - self.time_fun()
            log.debug("%s blocking %s s until %s", self, delta, self._run_at)
            try:
                await self.sleep_fun(delta)
            except asyncio.CancelledError:
                if not self._cancelled:
                    log.debug("%s interrupted", self)
                    pass
                else:
                    log.debug("%s cancelled and reopened", self)
                    raise
        log.debug("%s reopened", self)


@attr.s()
class DeferredTask(object):
    run = attr.ib(default=None)  # type: Callable[[],Coroutine]

    trigger_delay = attr.ib(default=10)  # type: int
    trigger_latch = attr.ib(default=Factory(DelayLatch))  # type: DelayLatch

    _task = attr.ib(init=False, default=None)  # type: Task

    @run.validator
    def __check_run(self, attribute, value):
        assert attribute.name is "run"
        if self.run and value and self.run is not value:
            raise ValueError("Can't set run method to %s via constructor argument as %s already provides a run method: %s" %
                             (value, self, self.run))
        if not self.run and not value:
            raise ValueError("No run method provided for %s %s" % (type(self), self))
        if not asyncio.iscoroutinefunction(self.run or value):
            raise ValueError("run method must be a coroutine function")

    @trigger_latch.validator
    def __check_latch(self, attribute, value):
        if not isinstance(value, DelayLatch):
            raise ValueError("%s.%s must be a DelayLatch, not %s '%s'" % (type(self), attribute, type(value), value))

    def _wait_for_task(self):
        """
        return a coroutine that completes once self._task is done, but don't consume or reraise its exception
        """
        return asyncio.wait([self._task])

    def is_pending(self):
        return self._task is not None and not self._task.done()

    def is_waiting(self):
        return not self.trigger_latch.is_open()

    def defer(self):
        if not self.is_pending():
            # task is neither waiting nor running, so start task
            self.trigger_latch.reopen_in(self.trigger_delay)
            self._task = execute_sequentially(self.trigger_latch.wait_on_latch, self.run)
        elif self.is_waiting():
            # task is waiting, delay further
            self.trigger_latch.reopen_in(self.trigger_delay)
        else:
            # task is currently running, but was started before call to defer()
            self.trigger_latch.reopen_in(self.trigger_delay)
            self._task = execute_sequentially(self._wait_for_task, self.trigger_latch.wait_on_latch, self.run)

    async def finalize(self):
        if not self.is_pending():
            # task is neither waiting nor running, manually call run() now
            self._task = execute_sequentially(self.run)
        elif self.is_waiting():
            # task is waiting, interrupt and run now
            self.trigger_latch.reopen_now()
        else:
            # task is currently running, but was started before call to finalize()
            self._task = execute_sequentially(self._wait_for_task, self.run)

        await self._task
