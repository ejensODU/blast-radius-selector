"""Build per-query line coverage and timing slices for the 8 heavy fixture-driven TestOptimizer tests."""
from __future__ import annotations

import concurrent.futures as futures
import gzip
import importlib
import inspect
import json
import os
import pickle
import sys
import time
import unittest
from unittest.mock import patch


TARGETS = [
    ("tests/test_optimizer.py::TestOptimizer::test_tpcds", "optimizer/tpc-ds/tpc-ds.sql", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", False),
    ("tests/test_optimizer.py::TestOptimizer::test_tpch", "optimizer/tpc-h/tpc-h.sql", "tests/fixtures/optimizer/tpc-h/tpc-h.sql", True),
    ("tests/test_optimizer.py::TestOptimizer::test_qualify_columns", "optimizer/qualify_columns.sql", "tests/fixtures/optimizer/qualify_columns.sql", True),
    ("tests/test_optimizer.py::TestOptimizer::test_annotate_funcs", "optimizer/annotate_functions.sql", "tests/fixtures/optimizer/annotate_functions.sql", False),
    ("tests/test_optimizer.py::TestOptimizer::test_simplify", "optimizer/simplify.sql", "tests/fixtures/optimizer/simplify.sql", True),
    ("tests/test_optimizer.py::TestOptimizer::test_merge_subqueries", "optimizer/merge_subqueries.sql", "tests/fixtures/optimizer/merge_subqueries.sql", True),
    ("tests/test_optimizer.py::TestOptimizer::test_optimize", "optimizer/optimizer.sql", "tests/fixtures/optimizer/optimizer.sql", True),
    ("tests/test_optimizer.py::TestOptimizer::test_unnest_subqueries", "optimizer/unnest_subqueries.sql", "tests/fixtures/optimizer/unnest_subqueries.sql", True),
]


def _install_inline_pool():
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


def _run_in_child(fn):
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            res = fn()
            data = pickle.dumps(res, protocol=5)
        except BaseException as err:
            data = pickle.dumps({"error": f"{type(err).__name__}: {err}"}, protocol=5)
        try:
            with os.fdopen(wfd, "wb") as wf:
                wf.write(data)
        except Exception:
            pass
        os._exit(0)
    os.close(wfd)
    chunks = []
    with os.fdopen(rfd, "rb") as rf:
        while True:
            b = rf.read(65536)
            if not b:
                break
            chunks.append(b)
    os.waitpid(pid, 0)
    return pickle.loads(b"".join(chunks))


def _trace_call(prefix, strip, fn):
    lines = set()
    mon = sys.monitoring
    tool = mon.COVERAGE_ID

    def on_line(code, line):
        name = code.co_filename
        if name.startswith(prefix):
            lines.add((name[strip:], line))
        return mon.DISABLE

    mon.use_tool_id(tool, "optslices2")
    mon.register_callback(tool, mon.events.LINE, on_line)
    began = time.process_time()
    mon.set_events(tool, mon.events.LINE)
    try:
        ok = fn()
    finally:
        mon.set_events(tool, 0)
        elapsed = time.process_time() - began
        mon.free_tool_id(tool)
    by_file = {}
    for name, line in lines:
        by_file.setdefault(name, []).append(line)
    return ok, elapsed, {k: sorted(v) for k, v in by_file.items()}


def trace_target(root: str, nodeid: str, rel: str, full_rel: str, needs_duckdb: bool) -> dict:
    prefix = root.rstrip("/") + "/sqlglot/"
    strip = len(root.rstrip("/")) + 1
    filename, classname, method = nodeid.split("::")
    module = importlib.import_module(filename[:-3].replace("/", "."))
    cls = getattr(module, classname)
    orig_loader = module.load_sql_fixture_pairs
    all_pairs = list(orig_loader(rel))
    size = len(all_pairs)

    # 1. Setup lines (in fresh forked child)
    def _do_setup():
        _, _, lns = _trace_call(prefix, strip, lambda: (cls.setUpClass(), True)[1])
        return lns

    setup_lines = _run_in_child(_do_setup)

    # 2. Outer lines (method run with rel fixture returning []) + capture check_file args
    def _do_outer():
        cls.setUpClass()
        captured = {}
        orig_cf = cls.check_file

        def spy_cf(self, file, func, *args, **kwargs):
            if f"optimizer/{file}.sql" == rel:
                captured["file"] = file
                captured["func_name"] = getattr(func, "__name__", None)
                captured["pretty"] = kwargs.get("pretty", args[0] if len(args) > 0 else False)
                captured["execute"] = kwargs.get("execute", args[1] if len(args) > 1 else None)
                captured["has_schema"] = "schema" in kwargs
                captured["schema"] = kwargs.get("schema")
                captured["extra_kwargs"] = {k: v for k, v in kwargs.items() if k not in ("pretty", "execute", "schema")}
            return orig_cf(self, file, func, *args, **kwargs)

        cls.check_file = spy_cf
        module.load_sql_fixture_pairs = lambda f, _orig=orig_loader: iter(()) if f == rel else _orig(f)
        res = unittest.TestResult()

        def run_it():
            cls(method).run(res)
            return res.testsRun == 1 and not res.errors and not res.failures

        ok, elapsed, lns = _trace_call(prefix, strip, run_it)
        return {"ok": ok, "elapsed": elapsed, "lines": lns, "captured": bool(captured), "uses_cf": bool(captured)}

    outer = _run_in_child(_do_outer)
    assert outer.get("ok"), f"outer failed for {nodeid}: {outer}"
    uses_cf = outer["uses_cf"]

    # 3. Trace each fixture pair i in its own isolated forked child (zero cross-query cache leakage).
    # For check_file methods with extra outer code, we run only check_file(rel, ...) for pair i;
    # for pure methods (test_annotate_funcs, test_tpcds, test_tpch, test_unnest_subqueries, test_merge_subqueries),
    # running the test method directly on [all_pairs[i]] is identical and fast!
    pure_methods = {"test_annotate_funcs", "test_tpcds", "test_tpch", "test_unnest_subqueries", "test_merge_subqueries", "test_optimize"}
    is_pure = method in pure_methods

    def _trace_batch(indices):
        out_batch = []
        for idx in indices:
            def _do_one(i=idx):
                if not needs_duckdb:
                    cls.pool = futures.ProcessPoolExecutor()
                    cls.conn = None
                else:
                    cls.setUpClass()
                module.load_sql_fixture_pairs = lambda f, _orig=orig_loader, _p=[all_pairs[i]]: (
                    iter(_p) if f == rel else _orig(f)
                )
                if is_pure:
                    res = unittest.TestResult()
                    def run_one():
                        cls(method).run(res)
                        return res.testsRun == 1 and not res.errors and not res.failures
                    ok, elapsed, lns = _trace_call(prefix, strip, run_one)
                else:
                    # test_qualify_columns or test_simplify: run only check_file for rel with [all_pairs[i]]
                    tc = cls(method)
                    tc.setUp()
                    if method == "test_qualify_columns":
                        def run_one():
                            with patch("sqlglot.generator.logger"):
                                tc.check_file("qualify_columns", module.qualify_columns, execute=True, schema=tc.schema)
                            return True
                    elif method == "test_simplify":
                        def run_one():
                            tc.check_file("simplify", module.simplify, schema=tc.schema)
                            return True
                    else:
                        raise RuntimeError(f"unexpected non-pure method {method}")
                    ok, elapsed, lns = _trace_call(prefix, strip, run_one)
                return {"idx": i, "ok": ok, "elapsed": elapsed, "lines": lns}

            r = _run_in_child(_do_one)
            if not r.get("ok"):
                raise RuntimeError(f"FAILED {nodeid}[{idx}]: {r}")
            out_batch.append(r)
        return out_batch

    # Fan out across 4 worker children (each forks isolated grandchildren BEFORE duckdb.connect!)
    n_workers = 4
    chunks = [list(range(w, size, n_workers)) for w in range(n_workers)]
    pipes = []
    pids = []
    for ch in chunks:
        if not ch:
            continue
        rfd, wfd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(rfd)
            try:
                res = _trace_batch(ch)
                data = pickle.dumps(res, protocol=5)
            except BaseException as err:
                data = pickle.dumps({"error": f"{type(err).__name__}: {err}"}, protocol=5)
            with os.fdopen(wfd, "wb") as wf:
                wf.write(data)
            os._exit(0)
        os.close(wfd)
        pipes.append(rfd)
        pids.append(pid)

    all_res = []
    for rfd, pid in zip(pipes, pids):
        buf = []
        with os.fdopen(rfd, "rb") as rf:
            while True:
                b = rf.read(65536)
                if not b:
                    break
                buf.append(b)
        os.waitpid(pid, 0)
        part = pickle.loads(b"".join(buf))
        if isinstance(part, dict) and "error" in part:
            raise RuntimeError(part["error"])
        all_res.extend(part)

    all_res.sort(key=lambda r: r["idx"])
    assert len(all_res) == size
    costs = [round(r["elapsed"] * 1000) for r in all_res]
    lines_mask: dict[str, dict[int, int]] = {}
    for r in all_res:
        bit = 1 << r["idx"]
        for fn, lns in r["lines"].items():
            fm = lines_mask.setdefault(fn, {})
            for ln in lns:
                fm[ln] = fm.get(ln, 0) | bit

    return {
        "kind": "check_file" if uses_cf else "loader",
        "file": full_rel,
        "rel": rel,
        "size": size,
        "setup_ms": 200,
        "outer_ms": round(outer["elapsed"] * 1000),
        "setup": {fn: set(lns) for fn, lns in setup_lines.items()},
        "outer": {fn: set(lns) for fn, lns in outer["lines"].items()},
        "cost": costs,
        "lines": lines_mask,
    }


def main():
    root = os.path.abspath(sys.argv[1])
    dest = os.path.abspath(sys.argv[2])
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, root)
    _install_inline_pool()
    # Pre-import base modules BEFORE any duckdb.connect() so forks are 100% thread-free.
    import duckdb  # noqa: F401
    import pandas  # noqa: F401
    import sqlglot  # noqa: F401
    import tests.test_optimizer  # noqa: F401

    out = {}
    t0 = time.time()
    for nodeid, rel, full_rel, needs_duckdb in TARGETS:
        ts = time.time()
        entry = trace_target(root, nodeid, rel, full_rel, needs_duckdb)
        out[nodeid] = entry
        print(
            f"sliced {nodeid.split('::')[-1]:22s}: size={entry['size']:4d} outer={entry['outer_ms']:3d}ms "
            f"sum_cost={sum(entry['cost']):5d}ms files={len(entry['lines']):3d} dt={time.time()-ts:.1f}s (tot {time.time()-t0:.1f}s)",
            flush=True,
        )
    with gzip.open(dest, "wb") as fh:
        pickle.dump(out, fh, protocol=5)
    print(f"wrote {dest} ({os.path.getsize(dest)}B) in {time.time()-t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
