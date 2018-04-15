import operator
from datetime import datetime
from typing import Any

import attr
import cattr

from studip_api.model import *
from studip_api.model import ModelConverter


def get_test_data():
    s = Semester("semester-id", "SS18", 0)
    c = Course("course-id", s, "1234V", "Testcourse", "V")
    r = File("root-id", c, parent=None, name="Testcourse", is_folder=True,
             is_single_child=True, is_accessible=True)
    f = File("file-id", c, parent=r, name="Testfile", is_folder=False,
             author="Tester", size=1234, created=datetime(2018, 4, 1, 12, 0), changed=datetime(2018, 4, 1, 14, 0),
             is_single_child=True, is_accessible=True)

    data = {
        "semester-id": s,
        "course-id": c,
        "root-id": r,
        "file-id": f,
    }
    return data


def get_repo_converter(backing_data, log):
    def structure_model_class(mc_data: Any, t) -> ModelClass:
        log.append((type(mc_data), t, mc_data))
        if isinstance(mc_data, t):
            return mc_data
        elif isinstance(mc_data, str):
            return backing_data[mc_data]
        else:
            return conv.structure_attrs_fromdict(mc_data, t)

    conv = ModelConverter(structure_model_class)
    return conv


def test_unstructure_eq_asdict():
    data = get_test_data()
    conv = cattr.Converter()
    assert (conv.unstructure(data) == {k: attr.asdict(v, recurse=True) for k, v in data.items()})


def test_reverse_simple():
    data = get_test_data()
    conv = register_forwardref_converter(register_datetime_converter(cattr.Converter()))
    unstructured = conv.unstructure(data)
    restructured = {
        "semester-id": conv.structure(unstructured["semester-id"], Semester),
        "course-id": conv.structure(unstructured["course-id"], Course),
        "root-id": conv.structure(unstructured["root-id"], File),
        "file-id": conv.structure(unstructured["file-id"], File),
    }

    assert data == restructured
    for k in data.keys():
        assert data[k] is not restructured[k]
    for d, op in (
            (data, operator.eq),
            (data, operator.is_),
            (restructured, operator.eq),
            (restructured, operator.is_not)):
        assert op(d["course-id"].semester, d["semester-id"])
        assert op(d["root-id"].course, d["course-id"])
        assert op(d["file-id"].course, d["course-id"])
        assert op(d["file-id"].parent, d["root-id"])


def test_reverse_model_conv_repo():
    data = get_test_data()
    restructured = {}
    log = []
    conv = get_repo_converter(restructured, log)

    unstructured = conv.unstructure(data)
    for key, t in zip(("semester-id", "course-id", "root-id", "file-id"), (Semester, Course, File, File)):
        restructured[key] = conv.structure(unstructured[key], t)

    assert [(f, t, d) for f, t, d in log if f == str] == \
           [(str, Semester, 'semester-id'), (str, Course, 'course-id'), (str, Course, 'course-id'), (str, File, 'root-id')]

    assert data == restructured
    for k in data.keys():
        assert data[k] is not restructured[k]
    for d in (data, restructured):
        assert d["course-id"].semester is d["semester-id"]
        assert d["root-id"].course is d["course-id"]
        assert d["file-id"].course is d["course-id"]
        assert d["file-id"].parent is d["root-id"]
