"""Record what one loop element of a fixture-driven test executes, alone in a fresh interpreter.

usage: slice_one.py <sqlglot root> <path::Class::test> <attribute> <index|setup> <out.json>

The test class is set up as usual, the class attribute holding the loaded
fixture list is cut down to the one element, and the test method runs under a
line recorder (sys.monitoring, each line reported once). Process-pool work runs
in this process through the same pickle boundary. "setup" records the class
set-up alone.
"""
import concurrent.futures as futures
import importlib
import json
import os
import pickle
import sys
import time
import unittest

root, nodeid, attr, which, dest = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
root = os.path.abspath(root)
os.chdir(root)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, root)
prefix = root.rstrip("/") + "/sqlglot/"
strip = len(root.rstrip("/")) + 1


class InlinePool(futures.Executor):
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        future = futures.Future()
        try:
            func, values, options = pickle.loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(func(*values, **options))))
        except BaseException as error:
            future.set_exception(error)
        return future


futures.ProcessPoolExecutor = InlinePool
filename, classname, method = nodeid.split("::")
module = importlib.import_module(filename[:-3].replace("/", "."))
cls = getattr(module, classname)

LINES = set()
mon = sys.monitoring
TOOL = mon.COVERAGE_ID


def on_line(code, line):
    name = code.co_filename
    if name.startswith(prefix):
        LINES.add((name[strip:], line))
    return mon.DISABLE


def start():
    mon.use_tool_id(TOOL, "slices")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.set_events(TOOL, mon.events.LINE)


def stop():
    mon.set_events(TOOL, 0)
    mon.free_tool_id(TOOL)


ok, seconds = True, 0.0
if which == "setup":
    start()
    cls.setUpClass()
    stop()
    size = len(getattr(cls, attr))
else:
    cls.setUpClass()
    values = getattr(cls, attr)
    size = len(values)
    setattr(cls, attr, [values[int(which)]])
    result = unittest.TestResult()
    began = time.process_time()
    start()
    cls(method).run(result)
    stop()
    seconds = time.process_time() - began
    ok = result.testsRun == 1 and not result.errors and not result.failures
try:
    cls.tearDownClass()
except Exception:
    pass
by_file = {}
for name, line in LINES:
    by_file.setdefault(name, []).append(line)
json.dump({"test": nodeid, "attr": attr, "which": which, "ok": ok, "size": size, "traced_cpu": round(seconds, 3),
           "lines": {name: sorted(v) for name, v in sorted(by_file.items())}}, open(dest, "w"))
