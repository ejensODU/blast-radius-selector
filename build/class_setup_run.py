"""Record how the tests of one class depend on files read by its setUpClass.

usage: class_setup_run.py <sqlglot root> <tests.json> <path::Class> <out.json>

A file opened only while the class is set up reaches a test through the class
attributes computed from it. This records, for the pinned commit:
  files    file -> class attributes named on the set-up statement that opened it
           ("*" when the statement names none)
  loaders  attribute -> fixture path, when the attribute is exactly
           list(tests.helpers.load_sql_fixture_pairs(path))
  touch    test -> set-up attributes it read (through a descriptor on the class)
  body     test -> files it opened itself
  unused   test -> attribute -> tuple positions of the loaded pairs it never
           inspected (measured by replacing that position with an object that
           records any use, and running the test again)
"""
import importlib
import json
import os
import re
import sys
import unittest

root, tests_json, class_id, dest = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
root = os.path.abspath(root)
os.chdir(root)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, root)
prefix = root.rstrip("/") + "/"
filename, classname = class_id.split("::")
module = importlib.import_module(filename[:-3].replace("/", "."))
cls = getattr(module, classname)
helpers = importlib.import_module("tests.helpers")

OPENED = []


def audit(event, args):
    if event == "open" and args and isinstance(args[0], str):
        path = os.path.abspath(args[0])
        if path.startswith(prefix):
            OPENED.append(path[len(prefix):])


sys.addaudithook(audit)

# ---- class set-up, statement by statement -------------------------------------
setup_code = cls.__dict__["setUpClass"].__func__.__code__
source = open(os.path.join(root, filename), encoding="utf-8").read().splitlines()
before = dict(vars(cls))
statements = []  # (line, files opened, attributes named or created)
state = {"line": None, "seen": dict((k, id(v)) for k, v in vars(cls).items())}


def close(line):
    if state["line"] is None:
        return
    files = sorted(set(OPENED))
    OPENED.clear()
    now = dict((k, id(v)) for k, v in vars(cls).items())
    fresh = {k for k, v in now.items() if state["seen"].get(k) != v}
    state["seen"] = now
    named = set(re.findall(r"\bcls\.(\w+)", source[state["line"] - 1]))
    statements.append((state["line"], files, sorted(fresh | named)))


def local(frame, event, arg):
    if frame.f_code is setup_code and event in ("line", "return"):
        close(frame.f_lineno)
        state["line"] = frame.f_lineno if event == "line" else None
    return local


def tracer(frame, event, arg):
    return local if frame.f_code is setup_code else None


sys.settrace(tracer)
try:
    cls.setUpClass()
finally:
    sys.settrace(None)
created = sorted(k for k in vars(cls) if k not in before)
files = {}
for line, opened, attrs in statements:
    for path in opened:
        attrs_here = [a for a in attrs if a in created]
        files.setdefault(path, set()).update(attrs_here or ["*"])

loaders = {}
for line, opened, attrs in statements:
    match = re.search(r"cls\.(\w+)\s*=\s*list\(load_sql_fixture_pairs\(([\"'])(.+?)\2\)\)", source[line - 1])
    if match and getattr(cls, match[1], None) == list(helpers.load_sql_fixture_pairs(match[3])):
        loaders[match[1]] = "tests/fixtures/" + match[3]

# ---- which set-up attributes each test reads ----------------------------------
TOUCHED = set()


class Watch:
    def __init__(self, name, value):
        self.name, self.value = name, value

    def __get__(self, instance, owner):
        TOUCHED.add(self.name)
        return self.value


originals = {name: vars(cls)[name] for name in created}


def install(overrides=None):
    for name, value in originals.items():
        setattr(cls, name, Watch(name, (overrides or {}).get(name, value)))


class Poison:
    used = False

    def _hit(self, *args, **kwargs):
        Poison.used = True
        raise RuntimeError("poisoned fixture field was used")

    __getattr__ = __str__ = __repr__ = __eq__ = __ne__ = __hash__ = __len__ = __iter__ = __bool__ = _hit
    __contains__ = __getitem__ = __add__ = __radd__ = __format__ = __reduce_ex__ = __lt__ = __gt__ = _hit


def run(method):
    TOUCHED.clear()
    OPENED.clear()
    result = unittest.TestResult()
    cls(method).run(result)
    ok = result.testsRun == 1 and not result.errors and not result.failures and not result.skipped
    return ok, sorted(TOUCHED), sorted(set(OPENED))


ids = [t["id"] for t in json.load(open(tests_json))["tests"] if t["id"].startswith(class_id + "::")]
touch, body, failed = {}, {}, []
install()
for nodeid in ids:
    ok, touched, opened = run(nodeid.split("::")[2])
    if not ok:
        failed.append(nodeid)
        continue
    touch[nodeid], body[nodeid] = touched, opened
    print(nodeid.split("::")[2], touched, len(opened), flush=True)

unused = {}
for attr, path in loaders.items():
    value = originals[attr]
    width = len(value[0])
    for nodeid in ids:
        if nodeid not in touch or attr not in touch[nodeid]:
            continue
        for position in range(width):
            Poison.used = False
            poisoned = [tuple(Poison() if i == position else x for i, x in enumerate(row)) for row in value]
            install({attr: poisoned})
            ok, _, _ = run(nodeid.split("::")[2])
            if ok and not Poison.used:
                unused.setdefault(nodeid, {}).setdefault(attr, []).append(position)
            print("poison", nodeid.split("::")[2], attr, position, ok, Poison.used, flush=True)
install()
for name, value in originals.items():
    setattr(cls, name, value)
cls.tearDownClass()
cls.doClassCleanups()
json.dump({"class": class_id, "created": created, "files": {k: sorted(v) for k, v in sorted(files.items())},
           "loaders": loaders, "touch": touch, "body": body, "unused": unused, "failed": failed},
          open(dest, "w"), indent=1, sort_keys=True)
