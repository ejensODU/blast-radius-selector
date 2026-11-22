"""Run one pool-using test in a fresh interpreter; record library lines executed while
unpickling what crosses the process-pool boundary (a failure there kills the pool)."""
import concurrent.futures as futures
import importlib
import json
import os
import pickle
import sys
import unittest

root, nodeid, dest = sys.argv[1], sys.argv[2], sys.argv[3]
os.chdir(root)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, root)
prefix = root.rstrip("/") + "/"
LINES = set()
CALLS = [0]


def _local(frame, event, arg):
    if event == "line":
        LINES.add((frame.f_code.co_filename, frame.f_lineno))
    return _local


def _tracer(frame, event, arg):
    if frame.f_code.co_filename.startswith(prefix):
        LINES.add((frame.f_code.co_filename, frame.f_lineno))
        return _local
    return None


def loads(blob):
    sys.settrace(_tracer)
    try:
        return pickle.loads(blob)
    finally:
        sys.settrace(None)


class InlinePool(futures.Executor):
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        CALLS[0] += 1
        future = futures.Future()
        try:
            func, values, options = loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(loads(pickle.dumps(func(*values, **options))))
        except BaseException as error:
            future.set_exception(error)
        return future


futures.ProcessPoolExecutor = InlinePool
filename, classname, method = nodeid.split("::")
module = importlib.import_module(filename[:-3].replace("/", "."))
case = getattr(module, classname)(method)
result = unittest.TestResult()
unittest.TestSuite([case]).run(result)
ok = not (result.errors or result.failures)
json.dump({"id": nodeid, "ok": ok, "calls": CALLS[0],
           "lines": sorted([f[len(prefix):], n] for f, n in LINES)}, open(dest, "w"))
