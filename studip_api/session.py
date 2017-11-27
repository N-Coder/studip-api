import asyncio
import logging
import os
import time
from tempfile import NamedTemporaryFile
from typing import List
from urllib.parse import urlencode

import aiofiles
import aiohttp
import more_itertools
from aiohttp import ClientError

from studip_api.parsers import *

log = logging.getLogger("studip_api.StudIPSession")
log_download = log.getChild("download")


class StudIPError(Exception):
    pass


class LoginError(StudIPError):
    pass


@attr.s(hash=False)
class StudIPSession:
    _user_name: str = attr.ib()
    _password: str = attr.ib(repr=False)
    _sso_base: str = attr.ib()
    _studip_base: str = attr.ib()

    def __attrs_post_init__(self):
        self._user_selected_semester: Semester = None
        self._user_selected_ansicht: str = None
        self._needs_reset_at: int = False
        self._semester_select_lock = asyncio.Lock()

    async def __aenter__(self):
        # self.http = requests.session()
        self.ahttp = await aiohttp.ClientSession().__aenter__()
        self.loop = asyncio.get_event_loop()
        await self._ado_login(self._user_name, self._password)
        return self

    async def __aexit__(self, *exc_info):
        await self.ahttp.__aexit__(*exc_info)

    def __hash__(self):
        return hash((self._user_name, self._password, self._sso_base, self._studip_base))

    def _sso_url(self, url):
        return self._sso_base + url

    def _studip_url(self, url):
        return self._studip_base + url

    async def _ado_login(self, user_name, password):
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
            return list(parse_semester_list(await r.text()))

    # TODO alternatively, parse list from "Farbgruppierung" and make further requests to get information for all courses

    async def get_courses(self, semester: Semester) -> List[Course]:
        if not self._user_selected_semester or not self._user_selected_ansicht:
            await self.get_semesters()
            assert self._user_selected_semester and self._user_selected_ansicht

        async with self._semester_select_lock:
            changed_ansicht = self._user_selected_ansicht != "sem_number"
            if changed_ansicht:
                await self.ahttp.post(
                    self._studip_url("/studip/dispatch.php/my_courses/store_groups"),
                    data={"select_group_field": "sem_number"})

            changed_semester = self._user_selected_semester != semester
            if changed_semester or changed_ansicht:
                self._needs_reset_at = self.loop.time() + 9
                self.loop.call_later(10, asyncio.ensure_future, self._reset_selections())

            async with self.ahttp.post(
                    self._studip_url("/studip/dispatch.php/my_courses/set_semester"),
                    data={"sem_select": semester.id}) as r:
                courses = list(parse_course_list(await r.text(), semester))
                return courses

    async def _reset_selections(self):
        if not self._needs_reset_at or self._needs_reset_at > self.loop.time():
            return
        async with self._semester_select_lock:
            if not self._needs_reset_at or self._needs_reset_at > self.loop.time():
                return

            await self.ahttp.post(
                self._studip_url("/studip/dispatch.php/my_courses/set_semester"),
                data={"sem_select": self._user_selected_semester})
            await self.ahttp.post(
                self._studip_url("/studip/dispatch.php/my_courses/store_groups"),
                data={"select_group_field": self._user_selected_ansicht})

            self._needs_reset_at = False

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

    async def download_file_contents(self, file: File, dest: str = None, chunk_size: int = 1024 * 256):
        if not dest:
            with NamedTemporaryFile(delete=False) as f:
                dest = f.name
        url = self._studip_url("/studip/sendfile.php?force_download=1&type=0&" \
                               + urlencode({"file_id": file.id, "file_name": file.name}))
        total_length = await self._fetch_total_length(url)

        async with aiofiles.open(dest, 'wb') as af:
            add_write_chunk(af)
            await af.truncate(total_length)

            # Calculating the start and the end index of each chunk
            ranges = list(more_itertools.sliced(range(total_length), chunk_size))
            requests = [self.ahttp.get(url, headers={"Range": "bytes={0}-{1}".format(i.start, i.stop)}) for i in ranges]
            writers = [_write_response(req, af, rnge, total_length) for req, rnge in zip(requests, ranges)]
            # TODO return Futures for separate ranges early
            done, pending = await asyncio.wait(writers)
            assert not pending

        timestamp = time.mktime(file.changed.timetuple())
        await self.loop.run_in_executor(None, os.utime, dest, (timestamp, timestamp))

        return dest

    async def _fetch_total_length(self, url):
        async with self.ahttp.head(url) as r:
            accept_ranges = r.headers.get("Accept-Ranges", "")
            if accept_ranges != "bytes":
                log_download.debug("Server is not indicating Accept-Ranges for file download:\n%s\n%s",
                                   r.request_info, r)
            total_length = r.content_length or r.headers.get("Content-Length", None)
            if not total_length and "Content-Range" in r.headers:
                content_range = r.headers["Content-Range"]
                log_download.debug("Stud.IP didn't send Content-Length but Content-Range '%s'", content_range)
                match = re.match("bytes ([0-9]*)-([0-9]*)/([0-9]*)", content_range)
                log_download.debug("Extracted Content-Length from Content-Range: %s => %s", match,
                                   match.groups() if match else "()")
                total_length = match.group(3)
            total_length = int(total_length)
        return total_length


async def _write_response(req, af, rnge, total_length):
    async with req as resp:
        requested_rage = resp.request_info.headers.get("Range", "")
        expected_range = "bytes %s-%s/%s" % (rnge.start, rnge.stop, total_length)
        actual_range = resp.headers.get("Content-Range", "")
        if expected_range != actual_range:  # FIXME this is off by one
            log_download.warning("Requested range %s, expected %s, got %s",
                                 requested_rage, expected_range, actual_range)

        offset = rnge.start
        while True:
            chunk, end_of_HTTP_chunk = await resp.content.readchunk()
            if not chunk:
                break
            log_download.debug("Chunk %s: writing at offset %6d + %6d new bytes = %6d new offset. Data: %s...%s",
                               actual_range, offset, len(chunk), offset + len(chunk), chunk[:10], chunk[-10:])
            written = await af.write_chunk(chunk, offset)
            offset += written

        log_download.debug("Chunk %s: wrote bytes from %6d to %6d", actual_range, rnge.start, offset)


def add_write_chunk(af):
    af._lock = asyncio.Lock()

    def _blocking_write_chunk(chunk, offset):
        log_download.debug("FH %s: writing at offset %6d + %6d new bytes = %6d new offset. Data: %s...%s",
                           af._file, offset, len(chunk), offset + len(chunk), chunk[:10], chunk[-10:])
        af._file.seek(offset)
        written = af._file.write(chunk)
        new_offset = af._file.tell()
        log_download.debug("FH %s: wrote   at offset %6d + %6d new bytes = %6d new offset",
                           af._file, offset, written, new_offset)
        assert written == len(chunk)
        assert new_offset == offset + written
        return written

    async def write_chunk(chunk, offset):
        async with af._lock:
            return await af._loop.run_in_executor(af._executor, _blocking_write_chunk, chunk, offset)

    af.write_chunk = write_chunk
    return af
