"""Build per-query line coverage and timing slices for TestOptimizer.test_tpcds and test_tpch."""
from __future__ import annotations

import concurrent.futures as futures
import importlib
import json
import os
import pickle
import subprocess
import sys
import time
import unittest


def run_one(root: str, nodeid: str, rel_file: str, which: str) -> dict:
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

    lines = set()
    mon = sys.monitoring
    tool = mon.COVERAGE_ID

    def on_line(code, line):
        name = code.co_filename
        if name.startswith(prefix):
            lines.add((name[strip:], line))
        return mon.DISABLE

    def start():
        mon.use_tool_id(tool, "optslices")
        mon.register_callback(tool, mon.events.LINE, on_line)
        mon.set_events(tool, mon.events.LINE)

    def stop():
        mon.set_events(tool, 0)
        mon.free_tool_id(tool)

    orig_loader = module.load_sql_fixture_pairs
    all_pairs = list(orig_loader(rel_file))
    size = len(all_pairs)
    ok, seconds = True, 0.0
    if which == "setup":
        start()
        cls.setUpClass()
        stop()
    else:
        idx = int(which)
        cls.setUpClass()
        module.load_sql_fixture_pairs = lambda f, _p=[all_pairs[idx]]: iter(_p)
        result = unittest.TestResult()
        began = time.process_time()
        start()
        cls(method).run(result)
        stop()
        seconds = time.process_time() - began
        ok = result.testsRun == 1 and not result.errors and not result.failures
        module.load_sql_fixture_pairs = orig_loader
    try:
        cls.tearDownClass()
    except Exception:
        pass
    by_file = {}
    for name, line in lines:
        by_file.setdefault(name, []).append(line)
    return {
        "ok": ok, "size": size, "traced_cpu": round(seconds, 4),
        "lines": {name: sorted(v) for name, v in sorted(by_file.items())},
    }


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--one":
        res = run_one(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
        print(json.dumps(res))
        return

    root = os.path.abspath(sys.argv[1])
    dest = os.path.abspath(sys.argv[2])
    targets = [
        ("tests/test_optimizer.py::TestOptimizer::test_tpcds", "optimizer/tpc-ds/tpc-ds.sql", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"),
        ("tests/test_optimizer.py::TestOptimizer::test_tpch", "optimizer/tpc-h/tpc-h.sql", "tests/fixtures/optimizer/tpc-h/tpc-h.sql"),
    ]
    out = {}
    t0 = time.time()
    for nodeid, rel, full_rel in targets:
        setup_res = json.loads(subprocess.check_output(
            [sys.executable, __file__, "--one", root, nodeid, rel, "setup"], text=True
        ))
        size = setup_res["size"]
        lines_mask: dict[str, dict[str, int]] = {}
        costs = []
        for i in range(size):
            r = json.loads(subprocess.check_output(
                [sys.executable, __file__, "--one", root, nodeid, rel, str(i)], text=True
            ))
            assert r["ok"], f"FAILED {nodeid}[{i}]"
            costs.append(round(r["traced_cpu"] * 1000))
            bit = 1 << i
            for fn, lns in r["lines"].items():
                fm = lines_mask.setdefault(fn, {})
                for ln in lns:
                    k = str(ln)
                    fm[k] = fm.get(k, 0) | bit
        out[nodeid] = {
            "kind": "check_file",
            "file": full_rel,
            "rel": rel,
            "size": size,
            "setup_ms": 200,
            "setup": setup_res["lines"],
            "cost": costs,
            "lines": lines_mask,
        }
        print(f"sliced {nodeid.split('::')[-1]}: size={size} sum_cost={sum(costs)}ms elapsed={time.time()-t0:.1f}s", flush=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"wrote {dest} ({os.path.getsize(dest)}B)", flush=True)


if __name__ == "__main__":
    main()
