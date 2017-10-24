import re
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


def parse_login_form(html):
    soup = BeautifulSoup(html, 'lxml')

    for form in soup.find_all('form'):
        if 'action' in form.attrs:
            return form.attrs['action']
    raise ParserError("LoginForm")


def parse_saml_form(html):
    soup = BeautifulSoup(html, 'lxml')
    saml_fields = ['RelayState', 'SAMLResponse']
    form_data = {}
    p = soup.find('p')
    if 'class' in p.attrs and 'form-error' in p.attrs['class']:
        raise ParserError('Error in Request')
    for input in soup.find_all('input'):
        if 'name' in input.attrs and 'value' in input.attrs and input.attrs['name'] in saml_fields:
            form_data[input.attrs['name']] = input.attrs['value']

    return form_data


def parse_semester_list(html):
    semester_list = []
    soup = BeautifulSoup(html, 'lxml')

    for item in soup.find_all('select'):
        if item.attrs['name'] == 'sem_select':
            optgroup = item.find('optgroup')
            for option in optgroup.find_all('option'):
                semester_list.append(Semester(option.attrs['value'], name=compact(option.contents[0])))

    for i, sem in enumerate(semester_list):
        sem.order = len(semester_list) - 1 - i

    return semester_list


def parse_course_list(html):
    course_list = []
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
                            course_list.append(Course(id=compact(get_url_field(link['href'], 'auswahl')),
                                                     semester=compact(semester),
                                                     number=current_number,
                                                     name=name, type=type, sync=SyncMode.NoSync))
                            break
    return course_list


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
