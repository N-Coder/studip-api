from datetime import datetime
from typing import Any, List

import attr

from studip_api.util import abbreviate_course_name, abbreviate_course_type


@attr.s
class Semester(object):
    id: str = attr.ib()
    name: str = attr.ib()
    order: int = attr.ib(default=-1)

    def __hash__(self):
        return hash(self.id)

    def __str__(self):
        return self.name

    def complete(self):
        return self.id and self.name and self.order >= 0


@attr.s
class Course(object):
    id: str = attr.ib()
    semester: Semester = attr.ib()
    number: int = attr.ib()
    name: str = attr.ib()
    type: str = attr.ib()

    # abbrev: str
    # type_abbrev: str

    def __hash__(self):
        return hash(self.id)

    def __str__(self):
        return "%s %s" % (self.number, self.name)

    @property
    def abbrev(self):
        return abbreviate_course_name(self.name)

    @property
    def type_abbrev(self):
        return abbreviate_course_type(self.type)

    def complete(self):
        return self.id and self.semester and self.number and self.name and self.type


@attr.s(hash=False)
class File(object):
    id: str = attr.ib()
    course: Course = attr.ib()
    parent: Any = attr.ib()
    name: str = attr.ib()
    author: str = attr.ib(default=None)
    description: str = attr.ib(default=None)
    size: int = attr.ib(default=None)
    created: datetime = attr.ib(default=None)
    changed: datetime = attr.ib(default=None)

    def __hash__(self):
        return hash(self.id)

    @property
    def path(self):
        return "%s/%s" % (self.parent.path, self.name)

    def __str__(self):
        return self.path

    def is_folder(self):
        return False

    def complete(self):
        return self.id and self.course and self.parent and self.name and self.changed


@attr.s(hash=False)
class Folder(File):
    contents: List[File] = attr.ib(default=None)

    @property
    def path(self):
        if self.is_root:
            return "/%s" % self.name
        else:
            return super().path

    @property
    def is_root(self):
        return not self.parent

    def is_folder(self):
        return True

    def complete(self):
        return self.contents is not None and super().complete()

    def __str__(self):
        if self.contents is None:
            return self.path + " (content unknown)"
        else:
            return self.path + "\n\t" + "\n\t".join(f.path for f in self.contents)
