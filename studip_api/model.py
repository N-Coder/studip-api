import re
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Type

import attr
import cattr
from attr import asdict

__all__ = ["ModelClass", "Semester", "Course", "File", "register_datetime_converter", "register_forwardref_converter",
           "register_model_converter", "ModelConverter"]

attrs = attr.s(hash=False, frozen=True)


@attrs
class ModelClass(object):
    id = attr.ib(type=str)

    def __hash__(self):
        return hash(self.id)


SEMESTER_RE = re.compile(r'^(SS|WS) (\d{2})(.(\d{2}))?')


@attrs
class Semester(ModelClass):
    name = attr.ib(type=str)
    order = attr.ib(default=-1, type=int)

    def __str__(self):
        return self.name

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


WORD_SEPARATOR_RE = re.compile(r'[-. _/()]+')
NUMBER_RE = re.compile(r'^([0-9]+)|([IVXLCDM]+)$')


@attrs
class Course(ModelClass):
    semester = attr.ib(type=Semester)
    number = attr.ib(type=str)
    name = attr.ib(type=str)
    type = attr.ib(type=str)

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


@attrs
class File(ModelClass):
    course = attr.ib(type=Course)
    parent = attr.ib(repr=False, type=Optional["File"])
    name = attr.ib(type=str)
    is_folder = attr.ib(type=bool)

    author = attr.ib(default=None, type=Optional[str])
    description = attr.ib(default=None, type=Optional[str])
    size = attr.ib(default=None, type=Optional[int])
    created = attr.ib(default=None, type=Optional[datetime])
    changed = attr.ib(default=None, type=Optional[datetime])
    is_single_child = attr.ib(default=False, type=bool)
    is_accessible = attr.ib(default=True, type=bool)

    def __str__(self):
        return "/".join(self.path)

    @property
    def path(self):
        if self.parent:
            return self.parent.path + [self.name]
        else:
            return [self.name]

    @property
    def is_root(self):
        return self.parent is None


def register_model_converter(outer_conv: cattr.Converter, nested_conv: cattr.Converter,
                             structure_model_class: Callable[[Any, Type[ModelClass]], ModelClass]) -> cattr.Converter:
    def unstructure_outer_model_class(obj: ModelClass) -> Dict:
        return nested_conv.unstructure(asdict(obj, recurse=False))  # use nested_conv for attr.s values

    def unstructure_nested_model_class(obj: ModelClass) -> Dict:
        return obj.id  # only write id of nested ModelClass instances

    outer_conv.register_structure_hook(ModelClass, structure_model_class)
    outer_conv.register_unstructure_hook(ModelClass, unstructure_outer_model_class)
    nested_conv.register_structure_hook(ModelClass, structure_model_class)
    nested_conv.register_unstructure_hook(ModelClass, unstructure_nested_model_class)

    return outer_conv


def register_forwardref_converter(conv: cattr.Converter) -> cattr.Converter:
    from typing import _ForwardRef

    def is_forwardref(t):
        return type(t) == _ForwardRef

    def structure_forwardref(value, fwdreft):
        return conv.structure(value, fwdreft._eval_type(globals(), locals()))

    conv.register_structure_hook_func(is_forwardref, structure_forwardref)
    return conv


def register_datetime_converter(conv: cattr.Converter) -> cattr.Converter:
    DATE_FORMATS = ['%d.%m.%Y %H:%M:%S', '%d/%m/%y %H:%M:%S']

    def structure_datetime(data: Any, t) -> datetime:
        exc = None

        assert t == datetime
        if isinstance(data, datetime):
            return data
        elif isinstance(data, (int, float)):
            return datetime.fromtimestamp(data)
        elif isinstance(data, str):
            for fmt in DATE_FORMATS:
                try:
                    return datetime.strptime(data, fmt)
                except ValueError as e:
                    exc = e

        raise ValueError("can't convert non-datetime value %s of type %s" % (data, data.__class__)) from exc

    def unstructure_datetime(dtvalue: datetime) -> float:
        return dtvalue.timestamp()

    conv.register_structure_hook(datetime, structure_datetime)
    conv.register_unstructure_hook(datetime, unstructure_datetime)
    return conv


def ModelConverter(structure_model_class):
    return register_model_converter(
        register_forwardref_converter(register_datetime_converter(cattr.Converter())),
        register_forwardref_converter(register_datetime_converter(cattr.Converter())),
        structure_model_class
    )
