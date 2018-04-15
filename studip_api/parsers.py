import re
import urllib.parse as urlparse
import warnings
from typing import Iterable, List

import attr
import cattr
from bs4 import BeautifulSoup

from studip_api import model
from studip_api.model import Course, File, Semester

__all__ = ["ParserError", "Parser"]

DUPLICATE_TYPE_RE = re.compile(r'^(?P<type>(Plenarü|Tutorü|Ü)bung(en)?|Tutorium|Praktikum'
                               + r'|(Obers|Haupts|S)eminar|Lectures?|Exercises?)(\s+(f[oü]r|on|zu[rm]?|i[nm]|auf))?'
                               + r'\s+(?P<name>.+)')
COURSE_NAME_TYPE_RE = re.compile(r'(.*?)\s*\(\s*([^)]+)\s*\)\s*$')


def compact(str):
    return " ".join(str.split())


def get_url_field(url, field):
    parsed_url = urlparse.urlparse(url)
    query = urlparse.parse_qs(parsed_url.query, encoding="iso-8859-1")
    return query[field][0] if field in query else None


def get_file_id_from_url(url):
    return re.findall("/studip/dispatch\.php/(course/files/index|file/details)/([a-z0-9]+)\?", url)[0][1]


def find_message(soup):
    mb = soup.find("div", class_="messagebox")
    if mb:
        mb = mb.extract()
        mbb = mb.find("div", class_="messagebox_buttons")
        if mbb:
            mbb.decompose()
        return mb.attrs["class"].remove("messagebox"), " ".join(mb.stripped_strings)
    return None, None


@attr.s(str=True, hash=False)
class ParserError(Exception):
    message = attr.ib()
    soup = attr.ib(repr=False, default=None)

    def __hash__(self):
        return hash((self.message, super().__hash__()))

    def dump(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w+t", delete=False, suffix="ParserError") as fp:
            fp.write(self.soup.prettify())
            return fp


@attr.s()
class Parser(object):
    converter = attr.ib(default=cattr.global_converter)  # type: cattr.Converter

    def parse_login_form(self, html):
        soup = BeautifulSoup(html, 'lxml')

        for form in soup.find_all('form'):
            if 'action' in form.attrs:
                return form.attrs['action']
        raise ParserError("Could not find login form", soup)

    def parse_saml_form(self, html):
        soup = BeautifulSoup(html, 'lxml')
        saml_fields = ['RelayState', 'SAMLResponse']
        form_data = {}
        p_error = soup.find('p', class_='form-error')
        if p_error:
            raise ParserError("Error in Request: '%s'" % p_error.text, soup)
        for input in soup.find_all('input'):
            if 'name' in input.attrs and 'value' in input.attrs and input.attrs['name'] in saml_fields:
                form_data[input.attrs['name']] = input.attrs['value']
        if not all(field in form_data.keys() for field in saml_fields):
            header = soup.find("header")
            if header:
                text = header.text.strip()
                if text != "Central Authentication Service":
                    raise ParserError("Could not extract SAMLResponse: '%s'" % text, soup)
            raise ParserError("Could not extract SAMLResponse", soup)

        return form_data

    def parse_user_selection(self, html):
        soup = BeautifulSoup(html, 'lxml')

        selected_semester = soup.find('select', {'name': 'sem_select'}).find('option', {'selected': True})
        if not selected_semester:
            # default to first if none is selected
            selected_semester = soup.find('select', {'name': 'sem_select'}).find('option')
        selected_semester = selected_semester.attrs['value']
        selected_ansicht = soup.find(
            'a', class_="active",
            href=re.compile("my_courses/store_groups\?select_group_field")
        ).attrs['href']

        return selected_semester, get_url_field(selected_ansicht, "select_group_field")

    def parse_semester_list(self, html) -> Iterable[Semester]:
        soup = BeautifulSoup(html, 'lxml')

        for item in soup.find_all('select', {'name': 'sem_select'}):
            options = item.find('optgroup').find_all('option')
            for i, option in enumerate(options):
                yield self.converter.structure(
                    dict(id=option.attrs['value'], name=compact(option.contents[0]), order=len(options) - 1 - i),
                    Semester
                )

    def parse_course_list(self, html, semester: model.Semester) -> Iterable[Course]:
        soup = BeautifulSoup(html, 'lxml')
        current_number = semester_str = None
        invalid_semester = found_course = False

        for item in soup.find_all('div', {'id': 'my_seminars'}):
            semester_str = item.find('caption').text.strip()
            if not semester_str == semester.name:
                invalid_semester = True
                warnings.warn(
                    "Ignoring courses for %s found while searching for the courses for %s" % (semester_str, semester.name))
                continue

            for tr in item.find_all('tr'):
                if 'class' in tr.attrs:
                    continue

                for td in tr.find_all('td'):
                    if len(td.attrs) == 0 and len(td.find_all()) == 0 and td.text.strip():
                        current_number = td.text.strip()

                    link = td.find('a')
                    if not link:
                        continue
                    full_name = compact(link.contents[0])
                    name, course_type = COURSE_NAME_TYPE_RE.match(full_name).groups()
                    match = DUPLICATE_TYPE_RE.match(name)
                    if match:
                        course_type = match.group("type")
                        name = match.group("name")
                    found_course = True
                    yield self.converter.structure(
                        dict(
                            id=get_url_field(link['href'], 'auswahl').strip(),
                            semester=semester,
                            number=current_number,
                            name=name, type=course_type
                        ), Course
                    )
                    break

        if invalid_semester and not found_course:
            raise ParserError("Only found courses for %s while searching for the courses for %s"
                              % (semester_str, semester.name), soup)

    def _parse_file_list(self, html):
        soup = BeautifulSoup(html, 'lxml')
        file_table = soup.find("table", class_="documents")
        if not file_table:
            msg = "Couldn't find document table. "
            clazz, error = find_message(soup)
            if error:
                msg += error
            raise ParserError(msg, soup)

        folder_id = file_table.attrs["data-folder_id"]
        caption_paths = file_table.find("caption").find("div", class_="caption-container").find_all("a")
        paths = [(get_file_id_from_url(a.attrs["href"]), a.text.strip()) for a in caption_paths]

        if paths[-1][0] != folder_id:
            raise ParserError("path %s doesn't end with folder_id %s" % (paths, folder_id), soup)
        is_root = len(paths) == 1
        folder_name = paths[-1][1]
        assert folder_id == paths[-1][0]
        parent_folder_id = paths[-2][0] if not is_root else None

        return file_table, folder_id, folder_name, is_root, parent_folder_id

    def parse_course_root_file(self, html, course: model.Course) -> File:
        file_table, folder_id, folder_name, is_root, parent_folder_id = self._parse_file_list(html)
        assert is_root

        return self.converter.structure(dict(
            id=folder_id, name=folder_name, course=course, parent=None, is_folder=True, is_single_child=True
        ), File)

    def parse_folder_file_list(self, html, parent_folder: File) -> Iterable[File]:
        file_table, folder_id, folder_name, is_root, parent_folder_id = self._parse_file_list(html)
        assert is_root or parent_folder_id == parent_folder.id

        files = []
        for tbody in file_table.find_all("tbody"):
            is_folder = {"subfolders": True, "files": False}[tbody.attrs["class"][0]]

            for tr in tbody.find_all('tr'):
                trid = tr.attrs.get("id", "")
                if not trid.startswith("row_folder_") and not trid.startswith("fileref_"):
                    continue
                tds = tr.find_all("td")

                file_data = dict(is_folder=is_folder, parent=parent_folder, course=parent_folder.course)

                checkbox = tds[0].find("input", class_="document-checkbox")
                if not checkbox:
                    warnings.warn("Can't download file %s in folder %s, trying to get data anyways" % (trid, parent_folder))
                    file_data["id"] = get_file_id_from_url(tds[6].find('a', {"data-dialog": "1"}).attrs["href"])
                    file_data["is_readable"] = False
                else:
                    file_data["id"] = checkbox.attrs["value"]
                    file_data["is_readable"] = True

                # icon = tds[1].find("img")
                file_data["name"] = tds[2].text.strip()
                file_data["size"] = tds[3].attrs['data-sort-value']
                file_data["author"] = tds[4].text.strip()
                file_data["changed"] = tds[5].attrs['title']

                files.append(file_data)

        if len(files) == 1:
            files[0]["is_single_child"] = True
        assert not any(f["id"] == folder_id for f in files)
        return self.converter.structure(files, List[File])
