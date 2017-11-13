import asyncio
import os
import time
from typing import List
from urllib.parse import urlencode

import aiofiles
import aiohttp
from aiohttp import ClientError

from studip_api.parsers import *


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
            return list(parse_semester_list(await r.text()))

    async def get_courses(self, semester: Semester) -> List[Course]:
        async with self.ahttp.post(
                self._studip_url("/studip/dispatch.php/my_courses/set_semester"),
                data={"sem_select": semester.id}) as r:
            return list(parse_course_list(await r.text(), semester))

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

    async def fetch_file_content(self, file: File, dest: str):
        url = self._studip_url("/studip/sendfile.php?force_download=1&type=0&" \
                               + urlencode({"file_id": file.id, "file_name": file.name}))

        async with self.ahttp.get(url) as r:
            async with aiofiles.open(dest, 'wb') as f:
                while True:
                    chunk, end_of_HTTP_chunk = await r.content.readchunk()
                    if not chunk:
                        break
                    await f.write(chunk)

        timestamp = time.mktime(file.changed.timetuple())
        os.utime(dest, (timestamp, timestamp))
