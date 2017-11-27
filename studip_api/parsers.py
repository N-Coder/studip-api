import re
import urllib.parse as urlparse
import warnings
from datetime import datetime
from typing import Optional

import attr
from bs4 import BeautifulSoup
from more_itertools import flatten, one

from studip_api.model import Course, File, Folder, Semester

DUPLICATE_TYPE_RE = re.compile(r'^(?P<type>(Plenarü|Tutorü|Ü)bung(en)?|Tutorium|Praktikum'
                               + r'|(Obers|Haupts|S)eminar|Lectures?|Exercises?)(\s+(f[oü]r|on|zu[rm]?|i[nm]|auf))?'
                               + r'\s+(?P<name>.+)')
COURSE_NAME_TYPE_RE = re.compile(r'(.*?)\s*\(\s*([^)]+)\s*\)\s*$')

DATE_FORMATS = ['%d.%m.%Y %H:%M:%S', '%d/%m/%y %H:%M:%S']


def compact(str):
    return " ".join(str.split())


def get_url_field(url, field):
    parsed_url = urlparse.urlparse(url)
    query = urlparse.parse_qs(parsed_url.query, encoding="iso-8859-1")
    return query[field][0] if field in query else None


def get_file_id_from_url(url):
    return re.findall("/studip/dispatch\.php/course/files/index/([a-z0-9]+)\?", url)[0]


def parse_date(date: str):
    exc = None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date, fmt)
        except ValueError as e:
            exc = e
    raise ParserError('Invalid date format') from exc


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


def parse_login_form(html):
    soup = BeautifulSoup(html, 'lxml')

    for form in soup.find_all('form'):
        if 'action' in form.attrs:
            return form.attrs['action']
    raise ParserError("Could not find login form", soup)


def parse_saml_form(html):
    soup = BeautifulSoup(html, 'lxml')
    saml_fields = ['RelayState', 'SAMLResponse']
    form_data = {}
    p = soup.find('p')
    if 'class' in p.attrs and 'form-error' in p.attrs['class']:
        raise ParserError("Error in Request: '%s'" % p.text, soup)
    for input in soup.find_all('input'):
        if 'name' in input.attrs and 'value' in input.attrs and input.attrs['name'] in saml_fields:
            form_data[input.attrs['name']] = input.attrs['value']

    return form_data


def parse_user_selection(html):
    soup = BeautifulSoup(html, 'lxml')

    selected_semester = one(flatten(
        select.find('optgroup').find_all('option', {'selected': True})
        for select in soup.find_all('select', {'name': 'sem_select'})
    )).attrs['value']
    selected_ansicht = soup.find(
        'a', class_="active",
        href=re.compile("my_courses/store_groups\?select_group_field")
    ).attrs['href']

    return selected_semester, get_url_field(selected_ansicht, "select_group_field")


def parse_semester_list(html):
    soup = BeautifulSoup(html, 'lxml')

    for item in soup.find_all('select', {'name': 'sem_select'}):
        options = item.find('optgroup').find_all('option')
        for i, option in enumerate(options):
            yield Semester(
                id=option.attrs['value'], name=compact(option.contents[0]), order=len(options) - 1 - i
            )


def parse_course_list(html, semester: Semester):
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
                yield Course(
                    id=get_url_field(link['href'], 'auswahl').strip(),
                    semester=semester,
                    number=current_number,
                    name=name, type=course_type
                )
                break

    if invalid_semester and not found_course:
        raise ParserError("Only found courses for %s while searching for the courses for %s"
                          % (semester_str, semester.name), soup)


def parse_file_list_index(html, course: Course, folder_info: Optional[Folder]):
    soup = BeautifulSoup(html, 'lxml')
    table = soup.find("table", class_="documents")
    if not table:
        msg = "Couldn't find document table. "
        clazz, error = find_message(soup)
        if error:
            msg += error
        raise ParserError(msg, soup)
    folder_id = table.attrs["data-folder_id"]

    caption_paths = table.find("caption").find("div", class_="caption-container").find_all("a")
    paths = [(get_file_id_from_url(a.attrs["href"]), a.text.strip()) for a in caption_paths]

    assert paths[-1][0] == folder_id
    is_root = len(paths) == 1
    folder_name = paths[-1][1]
    parent_folder_id = paths[-2][0] if not is_root else None

    files = []
    if folder_info:
        assert folder_id == folder_info.id
        assert course == folder_info.course
        assert (parent_folder_id == folder_info.parent) or (parent_folder_id == folder_info.parent.id)
        assert folder_name == folder_info.name or not folder_info.name
        assert is_root == folder_info.is_root

        folder = folder_info
        folder.contents = files
    else:
        folder = Folder(id=folder_id, course=course, parent=None, name=paths[-1][1], contents=files)

    for tbody in table.find_all("tbody"):
        type = {"subfolders": Folder, "files": File}[tbody.attrs["class"][0]]

        for tr in tbody.find_all('tr'):
            trid = tr.attrs.get("id", "")
            if not trid.startswith("row_folder_") and not trid.startswith("fileref_"):
                continue
            tds = tr.find_all("td")

            fid = tds[0].find("input", class_="document-checkbox").attrs["value"]
            icon = tds[1].find("img")
            name = tds[2].text.strip()
            size = int(tds[3].attrs['data-sort-value'])
            author = tds[4].text.strip()
            changed = parse_date(tds[5].attrs['title'])  # TODO created?

            files.append(type(id=fid, course=course, parent=folder, name=name, author=author, changed=changed,
                              size=size))

    assert not any(f.id == folder_id for f in files)
    return folder


def parse_file_details(html, file):
    warnings.warn("Not implemented")
    return file


def find_message(soup):
    mb = soup.find("div", class_="messagebox")
    if mb:
        mb = mb.extract()
        mbb = mb.find("div", class_="messagebox_buttons")
        if mbb:
            mbb.decompose()
        return mb.attrs["class"].remove("messagebox"), " ".join(mb.stripped_strings)
    return None, None
