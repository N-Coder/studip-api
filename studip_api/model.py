import re
from datetime import datetime
from typing import Any, List

import attr

WORD_SEPARATOR_RE = re.compile(r'[-. _/()]+')
NUMBER_RE = re.compile(r'^([0-9]+)|([IVXLCDM]+)$')
SEMESTER_RE = re.compile(r'^(SS|WS) (\d{2})(.(\d{2}))?')


@attr.s(hash=False)
class Semester(object):
    id = attr.ib()  # type: str
    name = attr.ib()  # type: str
    order = attr.ib(default=-1)  # type: int

    def __hash__(self):
        return hash(self.id)

    def __str__(self):
        return self.name

    def complete(self):
        return self.id and self.name and self.order >= 0

    @property
    def start_date(self) -> datetime:
        match = SEMESTER_RE.match(self.name)
        return datetime(year=int("20" + match.group(2)), month={"SS": 4, "WS": 10}[match.group(1)], day=1, hour=0,
                        minute=0)

    @property
    def lexical_short(self):
        return SEMESTER_RE.sub(r'20\2\1', self.name)

    @property
    def lexical(self):
        return SEMESTER_RE.sub(r'20\2\1\4', self.name)


@attr.s(hash=False)
class Course(object):
    id = attr.ib()  # type: str
    semester = attr.ib()  # type: Semester
    number = attr.ib()  # type: int
    name = attr.ib()  # type: str
    type = attr.ib()  # type: str

    def __hash__(self):
        return hash(self.id)

    def __str__(self):
        return "%s %s" % (self.number, self.name)

    @property
    def abbrev(self):
        words = WORD_SEPARATOR_RE.split(self.name)
        number = ""
        abbrev = ""
        if len(words) > 1 and NUMBER_RE.match(words[-1]):
            number = words[-1]
            words = words[0:len(words) - 1]
        if len(words) < 3:
            abbrev = "".join(w[0: min(3, len(w))] for w in words)
        elif len(words) >= 3:
            abbrev = "".join(w[0] for w in words if len(w) > 0)
        return abbrev + number

    @property
    def type_abbrev(self):
        special_abbrevs = {
            "Arbeitsgemeinschaft": "AG",
            "Studien-/Arbeitsgruppe": "SG",
        }
        try:
            return special_abbrevs[self.type]
        except KeyError:
            abbrev = self.type[0]
            if self.type.endswith("seminar"):
                abbrev += "S"
            return abbrev

    def complete(self):
        return self.id and self.semester and self.number and self.name and self.type


@attr.s(hash=False)
class File(object):
    id = attr.ib()  # type: str
    course = attr.ib()  # type: Course
    parent = attr.ib()  # type: Any
    name = attr.ib()  # type: str
    author = attr.ib(default=None)  # type: str
    description = attr.ib(default=None)  # type: str
    size = attr.ib(default=None)  # type: int
    created = attr.ib(default=None)  # type: datetime
    changed = attr.ib(default=None)  # type: datetime
    is_single_child = attr.ib(default=False)  # type:bool

    def __hash__(self):
        return hash(self.id)

    @property
    def path(self):
        if self.parent:
            return self.parent.path + [self.name]
        else:
            return [self.name]

    def __str__(self):
        return "/".join(self.path)

    def is_folder(self):
        return False

    def complete(self):
        return self.id and self.course and self.parent and self.name and self.changed


@attr.s(hash=False)
class Folder(File):
    contents = attr.ib(default=None)  # type: List[File]

    @property
    def is_root(self):
        return not self.parent

    def is_folder(self):
        return True

    def complete(self):
        return self.contents is not None and super().complete()

    def __str__(self):
        if self.contents is None:
            return super().__str__() + " (content unknown)"
        else:
            return super().__str__() + " (%s children)" % len(self.contents)
            # "\n\t" + "\n\t".join(f.path for f in self.contents)
