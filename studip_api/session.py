import asyncio
import logging
import os
import time
from typing import List, Union
from urllib.parse import urlencode

import aiohttp
import attr
from aiohttp import ClientError

from studip_api.async_delay import DeferredTask, DelayLatch, await_idle
from studip_api.downloader import Download
from studip_api.model import Course, File, Folder, Semester
from studip_api.parsers import Parser, ParserError

log = logging.getLogger("studip_api.StudIPSession")


class StudIPError(Exception):
    pass


class LoginError(StudIPError):
    pass


@attr.s(hash=False)
class StudIPSession(object):
    # TODO clean up attributes of Session class

    _sso_base = attr.ib()  # type: str
    _studip_base = attr.ib()  # type: str
    _http_args = attr.ib()  # type: dict
    _loop = attr.ib()  # type: asyncio.AbstractEventLoop

    def __attrs_post_init__(self):
        if not self._loop:
            self._loop = asyncio.get_event_loop()
        self._user_selected_semester = None  # type: Semester
        self._user_selected_ansicht = None  # type: str
        self._semester_select_lock = asyncio.Lock(loop=self._loop)
        self._reset_selections_task = DeferredTask(
            run=self.__reset_selections, trigger_latch=DelayLatch(sleep_fun=await_idle))

        http_args = dict(self._http_args)
        connector = aiohttp.TCPConnector(loop=self._loop, limit=http_args.pop("limit"),
                                         keepalive_timeout=http_args.pop("keepalive_timeout"),
                                         force_close=http_args.pop("force_close"),
                                         verify_ssl=http_args.pop("verify_ssl", False))
        self.ahttp = aiohttp.ClientSession(connector=connector, loop=self._loop,
                                           read_timeout=http_args.pop("read_timeout"),
                                           conn_timeout=http_args.pop("conn_timeout"),
                                           cookies=http_args.pop("cookies", None),
                                           trace_configs=http_args.pop("trace_configs", None))
        if http_args:
            raise ValueError("Unknown http_args %s", http_args)

        self.parser = Parser()

    async def close(self):
        try:
            await self._reset_selections_task.finalize()
        finally:
            if self.ahttp:
                await self.ahttp.close()

    def _sso_url(self, url):
        return self._sso_base + url

    def _studip_url(self, url):
        return self._studip_base + url

    async def do_login(self, user_name, password):
        try:
            async with self.ahttp.get(self._studip_url("/studip/index.php?again=yes&sso=shib")) as r:
                r.raise_for_status()
                post_url = self.parser.parse_login_form(await r.text())
        except (ClientError, ParserError) as e:
            raise LoginError("Could not initialize Shibboleth SSO login") from e

        try:
            async with self.ahttp.post(
                    self._sso_url(post_url),
                    data={
                        "j_username": user_name,
                        "j_password": password,
                        "uApprove.consent-revocation": "",
                        "_eventId_proceed": ""
                    }) as r:
                r.raise_for_status()
                form_data = self.parser.parse_saml_form(await r.text())
        except (ClientError, ParserError) as e:
            raise LoginError("Shibboleth SSO login failed") from e

        try:
            async with self.ahttp.post(self._studip_url("/Shibboleth.sso/SAML2/POST"), data=form_data) as r:
                r.raise_for_status()
                await r.text()
                if not r.url.path.startswith("/studip"):
                    raise LoginError("Invalid redirect after Shibboleth SSO login to %s" % r.url)
        except ClientError as e:
            raise LoginError("Could not complete Shibboleth SSO login") from e

    async def get_semesters(self) -> List[Semester]:
        async with self.ahttp.get(self._studip_url("/studip/dispatch.php/my_courses")) as r:
            r.raise_for_status()
            selected_semester, selected_ansicht = self.parser.parse_user_selection(await r.text())
            self._user_selected_semester = self._user_selected_semester or selected_semester
            self._user_selected_ansicht = self._user_selected_ansicht or selected_ansicht
            log.debug("User selected semester %s in ansicht %s",
                      self._user_selected_semester, self._user_selected_ansicht)
            return list(self.parser.parse_semester_list(await r.text()))

    async def get_courses(self, semester: Semester) -> List[Course]:
        if not self._user_selected_semester or not self._user_selected_ansicht:
            await self.get_semesters()
            assert self._user_selected_semester and self._user_selected_ansicht

        async with self._semester_select_lock:
            change_ansicht = self._user_selected_ansicht != "sem_number"
            if change_ansicht:
                await self.__select_ansicht("sem_number")

            change_semester = self._user_selected_semester != semester.id
            if change_semester or change_ansicht:
                self._reset_selections_task.defer()

            return list(self.parser.parse_course_list(await self.__select_semester(semester.id), semester))

    async def __select_semester(self, semester):
        semester = semester or "current"
        async with self.ahttp.post(
                self._studip_url("/studip/dispatch.php/my_courses/set_semester"),
                data={"sem_select": semester}) as r:
            r.raise_for_status()
            selected_semester, selected_ansicht = self.parser.parse_user_selection(await r.text())
            assert selected_semester == semester, "Tried to select semester %s, but Stud.IP delivered semester %s" % \
                                                  (semester, selected_semester)
            return await r.text()

    async def __select_ansicht(self, ansicht):
        ansicht = ansicht or "sem_number"
        async with self.ahttp.post(
                self._studip_url("/studip/dispatch.php/my_courses/store_groups"),
                data={"select_group_field": ansicht}) as r:
            r.raise_for_status()
            selected_semester, selected_ansicht = self.parser.parse_user_selection(await r.text())
            assert selected_ansicht == ansicht, "Tried to select ansicht %s, but Stud.IP delivered ansicht %s" % \
                                                (ansicht, selected_ansicht)
            return await r.text()

    async def __reset_selections(self):
        async with self._semester_select_lock:
            if not self.ahttp or self.ahttp.closed:
                return

            if self._user_selected_semester:
                await self.__select_semester(self._user_selected_semester)
            if self._user_selected_ansicht:
                await self.__select_ansicht(self._user_selected_ansicht)

    async def get_course_files(self, course: Course) -> Folder:
        async with self.ahttp.get(self._studip_url("/studip/dispatch.php/course/files/index?cid=" + course.id)) as r:
            r.raise_for_status()
            return self.parser.parse_file_list_index(await r.text(), course, None)

    async def get_folder_files(self, folder: Folder) -> Folder:
        async with self.ahttp.get(
                self._studip_url("/studip/dispatch.php/course/files/index/%s?cid=%s" % (folder.id, folder.course.id))
        ) as r:
            r.raise_for_status()
            return self.parser.parse_file_list_index(await r.text(), folder.course, folder)

    async def get_file_info(self, file: File) -> File:
        async with self.ahttp.get(
                self._studip_url("/studip/dispatch.php/file/details/%s?cid=%s" % (file.id, file.course.id))
        ) as r:
            r.raise_for_status()
            return self.parser.parse_file_details(await r.text(), file)

    async def download_file_contents(self, studip_file: File, local_dest: str = None,
                                     chunk_size: int = 1024 * 256) -> Download:

        async def on_completed(download, result: Union[List[range], Exception]):
            if isinstance(result, Exception):
                log.warning("Download %s -> %s failed", studip_file, local_dest, exc_info=True)
            else:
                log.info("Completed download %s -> %s", studip_file, local_dest)

                val = 0
                for r in result:
                    assert r.start <= val, "Non-connected ranges: %s" % result
                    val = r.stop
                assert val == download.total_length, "Completed ranges %s don't cover file length %s" % \
                                                     (result, download.total_length)

                if studip_file.changed:
                    timestamp = time.mktime(studip_file.changed.timetuple())
                    await self._loop.run_in_executor(None, os.utime, local_dest, (timestamp, timestamp))
                else:
                    log.warning("Can't set timestamp of file %s :: %s, because the value wasn't loaded from Stud.IP",
                                studip_file, local_dest)

                return result

        log.info("Starting download %s -> %s", studip_file, local_dest)
        try:
            download = Download(self.ahttp, self._get_download_url(studip_file), local_dest, chunk_size)
            download.on_completed.append(on_completed)
            await download.start()
        except:
            log.warning("Download %s -> %s could not be started", studip_file, local_dest, exc_info=True)
            raise
        return download

    def _get_download_url(self, studip_file):
        return self._studip_url("/studip/sendfile.php?force_download=1&type=0&"
                                + urlencode({"file_id": studip_file.id, "file_name": studip_file.name}))
