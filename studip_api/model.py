import inspect
import logging
import re
from datetime import datetime
from itertools import chain
from typing import Dict, List, Optional

import attr
from attr import asdict, fields

WORD_SEPARATOR_RE = re.compile(r'[-. _/()]+')
NUMBER_RE = re.compile(r'^([0-9]+)|([IVXLCDM]+)$')
SEMESTER_RE = re.compile(r'^(SS|WS) (\d{2})(.(\d{2}))?')

__all__ = ["ModelObjectMeta", "ModelObject", "Semester", "Course", "File", "Folder"]
pyid = id
log = logging.getLogger("studip_api.model")


class ModelObjectMeta(type):
    TRACKED_CLASSES = {}  # type: Dict[str, "ModelObjectMeta"]

    def __new__(mcs, name, bases, attrs):
        log.debug("New class %s%s: %s", name, bases, attrs)
        track = (name != "ModelObject" and not any(hasattr(base, "INSTANCES") for base in bases))
        if track:
            log.debug("Tracking instances for %s", name)
            attrs["INSTANCES"] = {}
        cls = super(ModelObjectMeta, mcs).__new__(mcs, name, bases, attrs)
        if track:
            mcs.TRACKED_CLASSES[name] = cls
            cls.__tracked_class__ = cls
        return cls

    @classmethod
    def export_all_data(mcs):
        return {k: v.export_data() for k, v in mcs.TRACKED_CLASSES.items()}

    @classmethod
    def import_all_data(mcs, data, update=False):
        if data.keys() != mcs.TRACKED_CLASSES.keys():
            raise ValueError("Can't import keys %s, expected %s" % (data.keys(), mcs.TRACKED_CLASSES.keys()))
        for key in mcs.TRACKED_CLASSES.keys():
            mcs.TRACKED_CLASSES[key].import_data(data[key], update)


@attr.s(hash=False)
class ModelObject(object, metaclass=ModelObjectMeta):
    id = attr.ib()  # type: str

    def __new__(cls, id, *args, **kwargs):
        if cls == ModelObject:
            raise ValueError("Can't instantiate ModelObject")
        if id not in cls.INSTANCES:
            obj = object.__new__(cls)
            cls.INSTANCES[id] = obj
            log.debug("Create new %s instance for UID %s at %s", cls.__name__, id, pyid(obj))
            return obj
        else:
            log.debug("Tried to create new %s instance for UID %s, but instance already exists at %s. "
                      "Update will be made via automatic call to __init__.", cls.__name__, id, pyid(cls.INSTANCES[id]))

    def __hash__(self):
        return hash(self.id)

    @classmethod
    def get_or_create(cls, id, *args, **kwargs):
        if isinstance(id, cls):
            return cls.INSTANCES.setdefault(id.id, id)
        if isinstance(id, dict):
            if args or kwargs:
                raise ValueError("get_or_create %s either takes id as dict or kwargs, not both" % cls)
            kwargs = id
            id = kwargs.pop("id")
        if not isinstance(id, str):
            raise ValueError("invalid id %s of type %s" % (id, id.__class__))

        if id not in cls.INSTANCES:
            cls.INSTANCES[id] = cls(id, *args, **kwargs)
        else:
            log.debug("Reuse %s instance for UID %s at %s", cls.__name__, id, pyid(cls.INSTANCES[id]))

        return cls.INSTANCES[id]

    @classmethod
    def update_or_create(cls, id, *args, **kwargs):
        if isinstance(id, cls):
            id = asdict(id, recurse=False)
        if isinstance(id, dict):
            if args or kwargs:
                raise ValueError("update_or_create %s either takes id as dict or kwargs, not both" % cls)
            kwargs = id
            id = kwargs.pop("id")
        if not isinstance(id, str):
            raise ValueError("invalid id %s of type %s" % (id, id.__class__))

        if id not in cls.INSTANCES:
            obj = cls(id, *args, **kwargs)
            cls.INSTANCES[id] = obj
        else:
            obj = cls.INSTANCES[id]
            cls.update(id, obj, args, kwargs)

        return obj

    @classmethod
    def update(cls, id, obj, args, kwargs):
        log.debug("Update %s instance for UID %s at %s", cls.__name__, id, pyid(cls.INSTANCES[id]))
        bound_args = inspect.signature(cls).bind(id, *args, **kwargs)
        for a in fields(obj.__class__):
            if not a.init:
                continue

            attr_name = a.name
            if attr_name in bound_args.arguments:
                setattr(obj, attr_name, bound_args.arguments[attr_name])
                continue

            # deal with private attributes
            init_name = attr_name if attr_name[0] != "_" else attr_name[1:]
            if init_name in bound_args.arguments:
                setattr(obj, attr_name, bound_args.arguments[init_name])
                continue

    @classmethod
    def list_instances(cls):
        return cls.INSTANCES.values()

    @classmethod
    def export_data(cls):
        return [
            {
                k: transform(v)
                for k, v in asdict(obj, recurse=False).items()
            } for obj in cls.list_instances()
        ]

    @classmethod
    def import_data(cls, data, update=False):
        creator = cls.update_or_create if update else cls.get_or_create
        return [creator(obj) for obj in data]


def transform(v):
    if isinstance(v, datetime):
        return v.timestamp()
    elif isinstance(v, ModelObject):
        return v.id
    elif isinstance(v, (tuple, list, set)):
        return v.__class__(transform(vv) for vv in v)
    else:
        assert v is None or isinstance(v, (str, int, float, bool)), \
            "can't transform value %s of type %s" % (v, v.__class__)
        return v


def datetime_converter(data):
    if isinstance(data, datetime):
        return data
    elif isinstance(data, (int, float)):
        return datetime.fromtimestamp(data)
    else:
        assert data is None, "can't convert non-datetime value %s of type %s" % (data, data.__class__)
        return None


def file_get_or_create(id, *args, **kwargs):
    # indirection for File.parent: Optional[File] from within body of class File
    if id:
        return File.get_or_create(id, *args, **kwargs)
    else:
        return None


@attr.s(hash=False)
class Semester(ModelObject):
    name = attr.ib()  # type: str
    order = attr.ib(default=-1)  # type: int

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
class Course(ModelObject):
    semester = attr.ib(converter=Semester.get_or_create)  # type: Semester
    number = attr.ib()  # type: int
    name = attr.ib()  # type: str
    type = attr.ib()  # type: str

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
class File(ModelObject):
    course = attr.ib(converter=Course.get_or_create)  # type: Course
    parent = attr.ib(repr=False, converter=file_get_or_create)  # type: Optional["File"]
    name = attr.ib()  # type: str
    author = attr.ib(default=None)  # type: str
    description = attr.ib(default=None)  # type: str
    size = attr.ib(default=None)  # type: int
    created = attr.ib(default=None, converter=datetime_converter)  # type: datetime
    changed = attr.ib(default=None, converter=datetime_converter)  # type: datetime
    is_single_child = attr.ib(default=False)  # type:bool

    @property
    def path(self):
        if self.parent:
            return self.parent.path + [self.name]
        else:
            return [self.name]

    def __str__(self):
        return "/".join(self.path)

    @property
    def is_root(self):
        return self.parent is None

    def is_folder(self):  # TODO make property
        return False

    def complete(self):
        return self.id and self.course and self.parent and self.name and self.changed

    @classmethod
    def list_instances(cls):
        def traverse(inst):
            yield inst
            if isinstance(inst, Folder) and inst.contents:
                for cont in inst.contents:
                    yield from traverse(cont)

        # sort parents before contents, so that parent is known when child is instantiated
        instances = list(chain.from_iterable(traverse(i) for i in cls.INSTANCES.values() if i.is_root))
        assert len(instances) == len(cls.INSTANCES), "tried to sort %s instances of File for serialization, " \
                                                     "but only got %s" % (len(cls.INSTANCES), len(instances))
        return instances

    @classmethod
    def import_data(cls, data, update=False):
        assert cls in (File, Folder), "File.import_data doesn't work for class %s" % cls
        folder_creator = Folder.update_or_create if update else Folder.get_or_create
        file_creator = File.update_or_create if update else File.get_or_create

        instances = []
        for obj in data:
            if "contents" in obj:
                inst = folder_creator(contents=None, **{k: v for k, v in obj.items() if k != "contents"})
                instances.append((inst, obj["contents"]))
            else:
                inst = file_creator(obj)
                instances.append((inst, None))

        # deserialize contents after all files and folders were deserialized to break cyclic references
        for inst, contents in instances:
            if not isinstance(inst, Folder):
                continue

            if contents is not None and (inst.contents is None or update):
                inst.contents = [File.INSTANCES.get(uid) for uid in contents]

        return [inst for inst, contents in instances]


@attr.s(hash=False)
class Folder(File):
    contents = attr.ib(default=None, repr=False)  # type: Optional[List[File]]

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
