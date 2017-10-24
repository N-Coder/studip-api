import requests, re
from html.parser import HTMLParser
from enum import IntEnum
import urllib.parse as urlparse
from datetime import datetime
from bs4 import BeautifulSoup

from .database import Semester, Course, SyncMode, File
from .util import compact

DUPLICATE_TYPE_RE = re.compile(r'^(?P<type>(Plenarü|Tutorü|Ü)bung(en)?|Tutorium|Praktikum'
                               + r'|(Obers|Haupts|S)eminar|Lectures?|Exercises?)(\s+(f[oü]r|on|zu[rm]?|i[nm]|auf))?'
                               + r'\s+(?P<name>.+)')
COURSE_NAME_TYPE_RE = re.compile(r'(.*?)\s*\(\s*([^)]+)\s*\)\s*$')


def get_url_field(url, field):
    parsed_url = urlparse.urlparse(url)
    query = urlparse.parse_qs(parsed_url.query, encoding="iso-8859-1")
    return query[field][0] if field in query else None


class ParserError(Exception):
    def __init__(self, message=None):
        self.message = message

    def __repr__(self):
        return "ParserError({})".format(repr(self.message))


class StopParsing(Exception):
    pass


def create_parser_and_feed(parser_class, html):
    parser = parser_class()
    try:
        parser.feed(html)
    except StopParsing:
        pass

    return parser


class LoginFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.post_url = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and "action" in attrs:
            self.post_url = attrs["action"]
            raise StopParsing

    def is_complete(self):
        return self.post_url is not None


def parse_login_form(html):
    parser = create_parser_and_feed(LoginFormParser, html)
    if parser.is_complete():
        return parser
    else:
        raise ParserError("LoginForm")


class SAMLFormParser(HTMLParser):
    fields = ["RelayState", "SAMLResponse"]

    def __init__(self):
        super().__init__()
        self.form_data = {}
        self.in_error_p = False
        self.error = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "p" and "class" in attrs and "form-error" in attrs["class"]:
            self.in_error_p = True
        elif tag == "input" and "name" in attrs and "value" in attrs:
            if attrs["name"] in SAMLFormParser.fields:
                self.form_data[attrs["name"]] = attrs["value"]

        if self.is_complete():
            raise StopParsing

    def handle_endtag(self, tag):
        if tag == "p":
            self.in_error_p = False

    def handle_data(self, data):
        if self.in_error_p:
            self.error = data

    def is_complete(self):
        return all(f in self.form_data for f in SAMLFormParser.fields)


def parse_saml_form(html):
    parser = create_parser_and_feed(SAMLFormParser, html)
    if parser.is_complete():
        return parser.form_data
    else:
        raise ParserError(parser.error)


def parse_semester_list(html):
    semesterlist = []
    soup = BeautifulSoup(html, 'lxml')

    for item in soup.find_all('select'):
        if item.attrs['name'] == 'sem_select':
            optgroup = item.find('optgroup')
            for option in optgroup.find_all('option'):
                semesterlist.append(Semester(option.attrs['value'], name=compact(option.contents[0])))

    for i, sem in enumerate(semesterlist):
        sem.order = len(semesterlist) - 1 - i

    return semesterlist

    # parser = create_parser_and_feed(SemesterListParser, html)
    # for i, sem in enumerate(parser.semesters):
    #     sem.order = len(parser.semesters) - 1 - i
    # return parser


class CourseListParser(HTMLParser):
    State = IntEnum("State", "before_sem before_thead_end table_caption before_tr "
                             "tr td_group td_img td_id td_name after_td a_name")

    def __init__(self):
        super().__init__()
        State = CourseListParser.State
        self.state = State.before_sem
        self.courses = []
        self.current_id = None
        self.current_number = None
        self.current_name = None

    def handle_starttag(self, tag, attrs):
        State = CourseListParser.State
        if self.state == State.before_sem:
            if tag == "div" and ("id", "my_seminars") in attrs:
                self.state = State.before_thead_end
        elif self.state == State.before_thead_end:
            if tag == "caption":
                self.state = State.table_caption
                self.current_semester = ""
        elif self.state == State.before_tr and tag == "tr":
            self.state = State.tr
            self.current_url = self.current_number = self.current_name = ""
        elif tag == "td" and self.state in [State.tr, State.td_group, State.td_img, State.td_id,
                                            State.td_name]:
            self.state = State(int(self.state) + 1)
        elif self.state == State.td_name and tag == "a":
            attrs = dict(attrs)
            self.current_id = get_url_field(attrs["href"], "auswahl")
            self.state = State.a_name

    def handle_endtag(self, tag):
        State = CourseListParser.State
        if tag == "div" and self.state != State.before_sem:
            raise StopParsing
        elif self.state == State.before_thead_end:
            if tag == "thead":
                self.state = State.before_tr
        elif self.state == State.table_caption:
            if tag == "caption":
                self.state = State.before_thead_end
        elif self.state == State.a_name:
            if tag == "a":
                self.state = State.td_name
        elif self.state == State.after_td:
            if tag == "tr":
                full_name = compact(self.current_name)
                name, type = COURSE_NAME_TYPE_RE.match(full_name).groups()
                match = DUPLICATE_TYPE_RE.match(name)
                if match:
                    type = match.group("type")
                    name = match.group("name")
                self.courses.append(Course(id=self.current_id,
                                           semester=compact(self.current_semester),
                                           number=compact(self.current_number),
                                           name=name, type=type, sync=SyncMode.NoSync))
                self.state = State.before_tr

    def handle_data(self, data):
        State = CourseListParser.State
        if self.state == State.td_id:
            self.current_number += data
        elif self.state == State.a_name:
            self.current_name += data
        elif self.state == State.table_caption:
            self.current_semester += data


def parse_course_list(html):
    courselist = []
    soup = BeautifulSoup(html, 'lxml')
    current_number = None

    for item in soup.find_all('div'):
        if 'id' in item.attrs and item.attrs['id'] == 'my_seminars':
            semester = item.find('caption').contents[0]
            for tr in item.find_all('tr'):
                if 'class' not in tr.attrs:
                    for td in tr.find_all('td'):
                        if len(td.attrs) == 0 and len(td.find_all()) == 0 and len(td.contents) > 0:
                            current_number = td.contents[0]

                        if td.find('a') is not None:
                            link = td.find('a')
                            full_name = compact(link.contents[0])
                            name, type = COURSE_NAME_TYPE_RE.match(full_name).groups()
                            match = DUPLICATE_TYPE_RE.match(name)
                            if match:
                                type = match.group("type")
                                name = match.group("name")
                            courselist.append(Course(id=compact(get_url_field(link['href'], 'auswahl')),
                                                     semester=compact(semester),
                                                     number=current_number,
                                                     name=name, type=type, sync=SyncMode.NoSync))
                            break
    return courselist


class OverviewParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.locations = {}

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            if "href" in attrs and "folder.php" in attrs["href"]:
                self.locations["folder_url"] = attrs["href"]


def parse_overview(html):
    return create_parser_and_feed(OverviewParser, html).locations


def parse_file_list(html):
    soup = BeautifulSoup(html, 'lxml')
    file_meta = []
    for td in soup.find_all('td'):
        if 'data-sort-value' in td.attrs:
            for ele in td.contents:
                if not isinstance(ele, str) and 'href' in ele.attrs and 'file_' in ele.attrs['href']:
                    file_link = ele.attrs['href']
                    if "sendfile.php" in file_link:
                        file_id = get_url_field(file_link, "file_id")
                        date = ''
                        for other_tds in td.parent.find_all('td'):
                            if 'title' in other_tds.attrs:
                                date_str = other_tds.attrs['title']
                                try:
                                    date = datetime.strptime(date_str, "%d.%m.%Y %H:%M:%S")
                                    break
                                except:
                                    pass
                        file_meta.append((file_id, date))
    return file_meta


def parse_file_details(course_id, html):
    file = File(None)
    soup = BeautifulSoup(html, 'lxml')
    for td in soup.find_all('td'):
        if 'data-sort-value' in td.attrs:
            for ele in td.contents:
                if not isinstance(ele, str) and 'href' in ele.attrs and 'file_' in ele.attrs['href']:
                    file_link = ele.attrs['href']
                    if "sendfile.php" in file_link and 'zip=' not in file_link:
                        file.id = get_url_field(file_link, "file_id")
                        file_name_parts = get_url_field(file_link, "file_name").rsplit(".", 1)
                        file.name = file_name_parts[0]
                        file.extension = file_name_parts[1] if len(file_name_parts) > 1 else ""
                        for other_tds in td.parent.find_all('td'):
                            if 'title' in other_tds.attrs:
                                date_str = other_tds.attrs['title']
                                try:
                                    file.remote_date = datetime.strptime(date_str, "%d.%m.%Y %H:%M:%S")
                                    break
                                except ValueError:
                                    pass

    file.path = '.'
    for caption in soup.find_all('caption'):
        div = caption.find('div')
        if 'class' in div.attrs and div.attrs['class'][0] == 'caption-container':
            path = ''
            for link in div.contents[0].find_all('a'):
                path.join(link.contents[0])
            file.path = path.replace(' ', '').split(sep='/')

    for li in soup.find_all('li'):
        if 'class' in li.attrs and li.attrs['class'][0] == 'action-menu-item':
            img = li.find('img')
            if 'alt' not in img.attrs or img.attrs['alt'] != 'info-circle':
                continue

            link = li.find('a')
            if 'href' in link.attrs:
                file.description_url = link.attrs['href']
                break

    file.course = course_id
    return file


def parse_file_desciption(file, html):
    soup = BeautifulSoup(html, 'lxml')
    i = 0
    for td in soup.find_all('td'):
        if i == 7:
            file.author = compact(td.find('a').contents[0])
        i += 1
    for div in soup.find_all('div'):
        if 'id' in div.attrs and div.attrs['id'] == 'preview_container':
            file.description = div.find('article').contents[0]
    if file.complete():
        return file
    else:
        raise ParserError("FileDetails")
