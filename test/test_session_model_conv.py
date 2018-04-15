import asyncio
import json
import os
from collections import defaultdict, deque
from itertools import chain
from typing import Deque, List

from more_itertools import one

from studip_api.model import Course, File, ModelConverter, Semester
from studip_api.session import StudIPSession

loop = session = None


def setup_module(module):
    global loop, session

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_debug(True)

    session = StudIPSession(
        loop=loop, studip_base="https://studip.uni-passau.de", sso_base="https://sso.uni-passau.de", http_args={
            "read_timeout": 30,
            "conn_timeout": 30,
            "keepalive_timeout": 60,
            "limit": 10,
            "force_close": False,
            "verify_ssl": False
        })
    try:
        session.ahttp.cookie_jar.load("cookies.pickle")
    except:
        try:
            with open(os.path.expanduser("~/.config/Stud.IP-Fuse/.studip-pw")) as f:
                password = f.read()
        except FileNotFoundError:
            from getpass import getpass
            password = getpass()
        loop.run_until_complete(session.do_login("fink13", password))


def teardown_module(module):
    session.ahttp.cookie_jar.save("cookies.pickle")
    loop.run_until_complete(session.close())


def test_export_reimport_export_live_data():
    repo = None

    def structure_model_class(data, clazz):
        if isinstance(data, clazz):
            return data
        elif isinstance(data, str):
            return repo[data]
        else:
            return session.parser.converter.structure_attrs_fromdict(data, clazz)

    session.parser.converter = ModelConverter(structure_model_class)

    semesters = loop.run_until_complete(session.get_semesters())
    semester = one(s for s in semesters if s.id == "4cb8438b3057e71a627ab7e25d73ba75")
    courses = loop.run_until_complete(session.get_courses(semester))
    course = one(c for c in courses if c.id == "b307cdc24c65dc487be23a22b557a8c5")
    folder = loop.run_until_complete(session.get_course_files(course))
    files = [folder]  # type: List[File]

    unknown_contents = deque([folder])  # type: Deque[File]
    while unknown_contents:
        item = unknown_contents.pop()
        if item.is_folder:
            contents = loop.run_until_complete(session.get_folder_files(item))
            unknown_contents.extend(contents)
            files.extend(contents)

    repo = repo1 = {o.id: o for o in chain(semesters, courses, files)}
    data1 = {
        "semesters": semesters,
        "courses": courses,
        "files": files,
    }
    json1 = session.parser.converter.unstructure(data1)

    repo = repo2 = {}
    data2 = defaultdict(list)
    for t, cl in (("semesters", Semester), ("courses", Course), ("files", File)):
        for s in json1[t]:
            o = session.parser.converter.structure(s, cl)
            repo2[o.id] = o
            data2[t].append(o)
    json2 = session.parser.converter.unstructure(dict(data2))

    assert repo1 == repo2
    for k in repo1.keys():
        assert repo1[k] is not repo2[k]
        if isinstance(repo1[k], Course):
            assert repo1[k].semester is repo1[repo1[k].semester.id]
            assert repo2[k].semester is repo2[repo2[k].semester.id]
            assert repo1[k].semester is not repo2[k].semester
        if isinstance(repo1[k], File):
            assert repo1[k].course is repo1[repo1[k].course.id]
            assert repo2[k].course is repo2[repo2[k].course.id]
            assert repo1[k].course is not repo2[k].course
            if repo1[k].parent:
                assert repo1[k].parent is repo1[repo1[k].parent.id]
                assert repo2[k].parent is repo2[repo2[k].parent.id]
                assert repo1[k].parent is not repo2[k].parent

    assert data1 == data2
    assert json1 == json2

    with open("TAPL-SS18.json", "wt") as f:
        json.dump(json2, f)
