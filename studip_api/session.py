import asyncio
import logging
import os
import time
from typing import List
from urllib.parse import urlencode
from weakref import WeakSet

import aiohttp
from aiohttp import ClientError

from studip_api.downloader import Download
from studip_api.parsers import *

log = logging.getLogger("studip_api.StudIPSession")


class StudIPError(Exception):
    pass


class LoginError(StudIPError):
    pass


@attr.s(hash=False)
class StudIPSession:
    _sso_base = attr.ib()  # type: str
    _studip_base = attr.ib()  # type: str
    _http_args = attr.ib()  # type: dict
    _loop = attr.ib()  # type: asyncio.AbstractEventLoop

    def __attrs_post_init__(self):
        self._user_selected_semester = None  # type: Semester
        self._user_selected_ansicht = None  # type: str
        self._needs_reset_at = False  # type: int
        self._semester_select_lock = asyncio.Lock()
        self._background_tasks = WeakSet()  # TODO better management of (failing of) background tasks
        if not self._loop:
            self._loop = asyncio.get_event_loop()

        http_args = dict(self._http_args)
        connector = aiohttp.TCPConnector(loop=self._loop, limit=http_args.pop("limit"),
                                         keepalive_timeout=http_args.pop("keepalive_timeout"),
                                         force_close=http_args.pop("force_close"))
        self.ahttp = aiohttp.ClientSession(connector=connector, loop=self._loop,
                                           read_timeout=http_args.pop("read_timeout"),
                                           conn_timeout=http_args.pop("conn_timeout"))
        if http_args:
            raise ValueError("Unknown http_args %s", http_args)

    async def close(self):
        try:
            for task in self._background_tasks:
                task.cancel()
            await self.__reset_selections(force=True)
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
                post_url = parse_login_form(await r.text())
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
                form_data = parse_saml_form(await r.text())
        except (ClientError, ParserError) as e:
            raise LoginError("Shibboleth SSO login failed") from e

        try:
            async with self.ahttp.post(self._studip_url("/Shibboleth.sso/SAML2/POST"), data=form_data) as r:
                await r.text()
                if not r.url.path.startswith("/studip"):
                    raise LoginError("Invalid redirect after Shibboleth SSO login to %s" % r.url)
        except ClientError as e:
            raise LoginError("Could not complete Shibboleth SSO login") from e

    async def get_semesters(self) -> List[Semester]:
        async with self.ahttp.get(self._studip_url("/studip/dispatch.php/my_courses")) as r:
            selected_semester, selected_ansicht = parse_user_selection(await r.text())
            self._user_selected_semester = self._user_selected_semester or selected_semester
            self._user_selected_ansicht = self._user_selected_ansicht or selected_ansicht
            log.debug("User selected semester %s in ansicht %s",
                      self._user_selected_semester, self._user_selected_ansicht)
            return list(parse_semester_list(await r.text()))

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
                self._needs_reset_at = self._loop.time() + 9
                self._background_tasks.add(
                    self._loop.call_later(10, lambda: asyncio.ensure_future(self.__reset_selections(quiet=True)))
                )

            courses = list(parse_course_list(await self.__select_semester(semester.id), semester))
            return courses

    async def __select_semester(self, semester):
        semester = semester or "current"
        async with self.ahttp.post(
                self._studip_url("/studip/dispatch.php/my_courses/set_semester"),
                data={"sem_select": semester}) as r:
            selected_semester, selected_ansicht = parse_user_selection(await r.text())
            assert selected_semester == semester, "Tried to select semester %s, but Stud.IP delivered semester %s" % \
                                                  (semester, selected_semester)
            return await r.text()

    async def __select_ansicht(self, ansicht):
        ansicht = ansicht or "sem_number"
        async with self.ahttp.post(
                self._studip_url("/studip/dispatch.php/my_courses/store_groups"),
                data={"select_group_field": ansicht}) as r:
            selected_semester, selected_ansicht = parse_user_selection(await r.text())
            assert selected_ansicht == ansicht, "Tried to select ansicht %s, but Stud.IP delivered ansicht %s" % \
                                                (ansicht, selected_ansicht)
            return await r.text()

    async def __reset_selections(self, force=False, quiet=False):
        try:
            async with self._semester_select_lock:
                if not self.ahttp or self.ahttp.closed:
                    return
                if not force and (not self._needs_reset_at or self._needs_reset_at > self._loop.time()):
                    return

                if self._user_selected_semester:
                    await self.__select_semester(self._user_selected_semester)
                if self._user_selected_ansicht:
                    await self.__select_ansicht(self._user_selected_ansicht)

                self._needs_reset_at = False
        except:
            if quiet:
                log.warning("Could not reset semester selection", exc_info=True)
            else:
                raise

    async def get_course_files(self, course: Course) -> Folder:
        async with self.ahttp.get(self._studip_url("/studip/dispatch.php/course/files/index?cid=" + course.id)) as r:
            return parse_file_list_index(await r.text(), course, None)

    async def get_folder_files(self, folder: Folder) -> Folder:
        async with self.ahttp.get(
                self._studip_url("/studip/dispatch.php/course/files/index/%s?cid=%s" % (folder.id, folder.course.id))
        ) as r:
            return parse_file_list_index(await r.text(), folder.course, folder)

    async def get_file_info(self, file: File) -> File:
        async with self.ahttp.get(
                self._studip_url("/studip/dispatch.php/file/details/%s?cid=%s" % (file.id, file.course.id))
        ) as r:
            return parse_file_details(await r.text(), file)

    async def download_file_contents(self, studip_file: File, local_dest: str = None,
                                     chunk_size: int = 1024 * 256) -> Download:
        log.info("Starting download %s -> %s", studip_file, local_dest)
        download = Download(self.ahttp, self._get_download_url(studip_file), local_dest, chunk_size)
        await download.start()
        old_completed_future = download.completed

        async def await_completed():
            try:
                ranges = await old_completed_future
                log.info("Completed download %s -> %s", studip_file, local_dest)

                val = 0
                for r in ranges:
                    assert r.start <= val
                    val = r.stop
                assert val == download.total_length

                if studip_file.changed:
                    timestamp = time.mktime(studip_file.changed.timetuple())
                    await self._loop.run_in_executor(None, os.utime, local_dest, (timestamp, timestamp))
                else:
                    log.warning("Can't set timestamp of file %s :: %s, because the value wasn't loaded from Stud.IP",
                                studip_file, local_dest)

                return ranges
            except:
                log.warning("Download %s -> %s failed", studip_file, local_dest, exc_info=True)
                raise

        download.completed = asyncio.ensure_future(await_completed())
        return download

    def _get_download_url(self, studip_file):
        return self._studip_url("/studip/sendfile.php?force_download=1&type=0&"
                                + urlencode({"file_id": studip_file.id, "file_name": studip_file.name}))
