"""Run one test in this fresh interpreter with read tracking (v2: dict + set + frozenset + scalar class attrs)."""
import concurrent.futures as futures
import importlib
import json
import os
import pickle
import sys
import time
import unittest

root, nodeid, dest = sys.argv[1], sys.argv[2], sys.argv[3]
os.chdir(root)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/app")
import track
from worldfp import keytok

track.install(root)

POOL_USED = []


class InlinePool(futures.Executor):
    """Process-pool work runs here, through the same pickle boundary, so it is tracked."""

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        POOL_USED.append(1)
        future = futures.Future()
        try:
            func, values, options = pickle.loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(func(*values, **options))))
        except BaseException as error:
            future.set_exception(error)
        return future


futures.ProcessPoolExecutor = InlinePool
filename, classname, method = nodeid.split("::")
started = time.time()
outcome, detail = "ok", ""
try:
    module = importlib.import_module(filename[:-3].replace("/", "."))
    case = getattr(module, classname)(method)
    result = unittest.TestResult()
    unittest.TestSuite([case]).run(result)
    if result.errors or result.failures:
        outcome = "fail"
        detail = (result.errors + result.failures)[0][1][-1500:]
    elif result.skipped:
        outcome = "skip"
except BaseException as error:
    outcome, detail = "crash", f"{type(error).__name__}: {error}"[:1500]
reads = {}
for cell, key in track.READS:
    reads.setdefault(cell, set()).add(keytok(key))
prefix = root.rstrip("/") + "/"
imports = sorted({m.__file__[len(prefix):] for name, m in list(sys.modules.items())
                  if (name == "sqlglot" or name.startswith("sqlglot.")) and (getattr(m, "__file__", None) or "").startswith(prefix)})
json.dump({
    "id": nodeid, "outcome": outcome, "detail": detail, "seconds": round(time.time() - started, 3),
    "cells": [list(c) for c in track.CELLS],
    "reads": {str(c): sorted(v) for c, v in reads.items()},
    "bulk": sorted(track.BULK),
    "attr_cells": [list(c) for c in track.ATTR_CELLS],
    "attr_reads": sorted(track.ATTR_READS),
    "pool": bool(POOL_USED), "aliases": track.ALIASES, "stale": track.STALE, "imports": imports,
}, open(dest, "w"))
