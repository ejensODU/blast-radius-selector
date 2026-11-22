"""Run a small batch of unittest cases against one temporary, patched checkout."""

from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections import defaultdict
from functools import lru_cache
from pathlib import Path


# Tests after which a class-level process pool was found broken, from the last probe.
LAZY = False
BROKEN: list[str] = []
# Non-empty when the last probe ran every requested class to the end.
COMPLETE: list[bool] = []


def _overlay(original: Path, shadow: Path, changes: dict[str, str]) -> None:
    """Copy edited files and link unchanged siblings, expanding only edited directories."""
    branches = defaultdict(dict)
    for name, source in changes.items():
        first, slash, rest = name.partition("/")
        branches[first][rest if slash else ""] = source
    edited_stems = {
        name[:-3]
        for name, children in branches.items()
        if name.endswith(".py") and "" in children
    }
    for path in original.iterdir():
        if path.name in branches:
            continue
        # Changed files must never inherit pristine bytecode, even on a size/mtime collision.
        if path.name == "__pycache__" and edited_stems:
            shadow_cache = shadow / path.name
            shadow_cache.mkdir()
            suffix = "." + sys.implementation.cache_tag
            for bytecode in path.glob("*" + suffix + "*.pyc"):
                if bytecode.name.rpartition(suffix)[0] not in edited_stems:
                    (shadow_cache / bytecode.name).symlink_to(bytecode)
            continue
        (shadow / path.name).symlink_to(path)
    for name, descendants in branches.items():
        destination = shadow / name
        if "" in descendants:
            if len(descendants) != 1:
                raise ValueError("a file is also a directory")
            content = descendants[""]
            if isinstance(content, bytes):
                destination.write_bytes(content)
            else:
                destination.write_text(content, encoding="utf-8")
        else:
            destination.mkdir()
            _overlay(original / name, destination, descendants)


def _invoke(root, changes, values, timeout, mode):
    """Run the requested check with a hard wall clock budget and a private result channel."""
    if not values or not changes or timeout <= 0:
        return []
    if any(
        Path(p).is_absolute() or set(p.split("/")) & {"", ".", ".."} for p in changes
    ):
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="blast-") as directory:
            shadow = Path(directory)
            _overlay(root.resolve(), shadow, changes)
            child = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()), mode],
                cwd=shadow,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=dict(
                    os.environ,
                    SKIP_INTEGRATION="0",
                    PYTHONDONTWRITEBYTECODE="1",
                    PYTHONPATH=os.pathsep.join(
                        [str(shadow), os.environ.get("PYTHONPATH", "")]
                    ),
                ),
            )
            try:
                stdout, _ = child.communicate(
                    json.dumps(values).encode(), timeout=timeout
                )
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, _ = child.communicate()
            records = []
            for line in stdout.splitlines():
                try:
                    records.append(json.loads(line))
                except (UnicodeError, ValueError, TypeError):
                    continue
            return records
    except (OSError, ValueError):
        return []


@lru_cache(maxsize=128)
def _fixture_owners(path):
    """Identify classes with additional setup costs missing from per-method durations."""
    try:
        source = path.read_text(encoding="utf-8")
        if "setUpClass" not in source:
            return frozenset()
        tree = ast.parse(source)
    except (OSError, UnicodeError, SyntaxError):
        return frozenset()
    return frozenset(
        cls.name
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        and any(
            isinstance(n, ast.FunctionDef) and n.name == "setUpClass" for n in cls.body
        )
    )


def _multiple_setups(root, ids):
    groups = {tuple(parts[:2]) for node in ids if len(parts := node.split("::")) == 3}
    if len(groups) < 2:
        return False
    return (
        sum(name in _fixture_owners(root / filename) for filename, name in groups) >= 2
    )


def probe_slices(root: Path, changes: dict[str, str], spec: dict, timeout: float):
    """True when the test passes on the given elements of its fixture list, False when it
    fails, None when the run did not finish."""
    for record in _invoke(root, changes, spec, timeout, "slices"):
        if isinstance(record, dict) and "slices" in record:
            return record["slices"]
    return None


def probe_phase1(root: Path, changes: dict[str, str], specs: list[dict], timeout: float) -> dict[str, dict]:
    """Run deterministic Phase-1 dry-run checks for executor TPC tests in one fresh interpreter."""
    out = {}
    for record in _invoke(root, changes, specs, timeout, "phase1"):
        if isinstance(record, dict) and "test" in record and "phase1" in record:
            out[record["test"]] = record
    return out


def probe_tpc_csv(root: Path, changes: dict[str, bytes], spec: dict, timeout: float) -> dict | None:
    """Validate a binary TPC .csv.gz edit against TestExecutor setup and the affected queries."""
    for record in _invoke(root, changes, spec, timeout, "tpc_csv"):
        if isinstance(record, dict) and "setup_ok" in record:
            return record
    return None


def probe(
    root: Path, changes: dict[str, str], ids: list[str], timeout: float, skip_executor_setup: bool = False
) -> set[str]:
    """Confirm ordinary unittest methods. Exclude pytest-specific setup and marks."""
    if any(
        Path(p).name
        in {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}
        for p in changes
    ):
        return set()
    wanted = set(ids)
    passed = set()
    BROKEN.clear()
    COMPLETE.clear()
    groups = {tuple(node.split("::")[:2]) for node in ids}
    payload = {"ids": ids, "skip_executor_setup": skip_executor_setup}
    for record in _invoke(root, changes, payload, timeout, "tests"):
        if isinstance(record, list):
            passed.update(wanted.intersection(record))
        elif isinstance(record, dict) and record.get("broken") in wanted:
            BROKEN.append(record["broken"])
        elif isinstance(record, dict) and record.get("done") == len(groups):
            COMPLETE.append(True)
    return passed


def check_imports(
    root: Path, changes: dict[str, str], timeout: float = 2.0
) -> bool | None:
    """Import changed package modules in a fresh interpreter. None means inconclusive."""
    modules = []
    for path in changes:
        if not path.endswith(".py"):
            continue
        pieces = path[:-3].split("/")
        if pieces[-1] == "__init__":
            pieces.pop()
        if (
            not pieces
            or pieces[0] not in {"sqlglot", "tests"}
            or not all(s.isidentifier() for s in pieces)
        ):
            return None
        modules.append(".".join(pieces))
    records = _invoke(root, changes, sorted(set(modules)), timeout, "imports")
    return records[-1] if records and isinstance(records[-1], bool) else None


def _install_pool() -> None:
    """Run process-pool tasks in this process, keeping the pickle boundaries.

    A real pool only adds what the boundaries add: arguments are unpickled in a
    worker, results and exceptions are unpickled in the parent, and a worker that
    dies while unpickling leaves the pool broken for every later caller.
    """
    import concurrent.futures as futures
    import pickle
    from concurrent.futures.process import BrokenProcessPool

    class Deferred(futures.Future):
        _thunk = None

        def _settle(self):
            thunk, self._thunk = self._thunk, None
            if thunk is not None:
                thunk()

        def result(self, timeout=None):
            self._settle()
            return super().result(timeout)

        def exception(self, timeout=None):
            self._settle()
            return super().exception(timeout)

    class Pool(futures.Executor):
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
            self._broken = False
            self._initializer = (initializer, initargs)
            self._started = False

        def _break(self, future):
            self._broken = True
            future.set_exception(BrokenProcessPool("A child process terminated abruptly, the process pool is not usable anymore"))
            return future

        def submit(self, fn, /, *args, **kwargs):
            if self._broken:
                raise BrokenProcessPool("A child process terminated abruptly, the process pool is not usable anymore")
            future = futures.Future()
            if not self._started:
                self._started = True
                initializer, initargs = self._initializer
                if initializer is not None:
                    try:
                        initializer(*pickle.loads(pickle.dumps(initargs)))
                    except BaseException:
                        return self._break(future)
            try:
                payload = pickle.dumps((fn, args, kwargs))
            except BaseException as error:
                future.set_exception(error)
                return future
            if LAZY:
                # Run the task when its result is asked for, so a failing element
                # stops the test before the remaining ones are computed.
                future = Deferred()
                future._thunk = lambda: self._execute(payload, future)
                return future
            return self._execute(payload, future)

        def _execute(self, payload, future):
            try:
                function, values, options = pickle.loads(payload)
            except BaseException:
                return self._break(future)
            try:
                outcome, failed = function(*values, **options), False
            except BaseException as error:
                outcome, failed = error, True
            try:
                packed = pickle.dumps(outcome)
            except BaseException as error:
                future.set_exception(error)
                return future
            try:
                outcome = pickle.loads(packed)
            except BaseException:
                return self._break(future)
            if failed:
                future.set_exception(outcome)
            else:
                future.set_result(outcome)
            return future

        def shutdown(self, wait=True, *, cancel_futures=False):
            return None

    futures.ProcessPoolExecutor = Pool


def _run() -> None:
    global LAZY
    import importlib
    import unittest

    _install_pool()

    # Keep application output away from the result channel, including native-library writes.
    out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.path.insert(0, os.getcwd())
    groups = defaultdict(list)
    values = json.load(sys.stdin)
    if sys.argv[-1] == "imports":
        try:
            for module_name in values:
                importlib.import_module(module_name)
        except Exception:
            print("false", file=out)
        else:
            print("true", file=out)
        return
    if sys.argv[-1] == "phase1":
        import hashlib
        import pickle
        from sqlglot import exp, find_tables, parse_one, transpile
        from sqlglot.executor.python import PythonExecutor
        from sqlglot.executor.table import Table, ensure_tables
        from sqlglot.optimizer import optimize
        from sqlglot.planner import Plan
        from sqlglot.schema import ensure_schema
        from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

        def _ast_fp(node, h):
            stack = [node]
            while stack:
                cur = stack.pop()
                if isinstance(cur, exp.Expr):
                    h.update(b"E:")
                    h.update(type(cur).__qualname__.encode())
                    h.update(b";")
                    if cur._type is not None:
                        h.update(b"T:")
                        stack.append(cur._type)
                    for k in sorted(cur.args):
                        h.update(b"K:")
                        h.update(k.encode())
                        h.update(b"=")
                        stack.append(cur.args[k])
                elif isinstance(cur, list):
                    h.update(b"L[")
                    h.update(str(len(cur)).encode())
                    h.update(b"]")
                    for x in reversed(cur):
                        stack.append(x)
                elif isinstance(cur, tuple):
                    h.update(b"P(")
                    h.update(str(len(cur)).encode())
                    h.update(b")")
                    for x in reversed(cur):
                        stack.append(x)
                else:
                    h.update(b"V:")
                    h.update(type(cur).__name__.encode())
                    h.update(b":")
                    h.update(repr(cur).encode())
                    h.update(b";")

        for job in values:
            test_id = job["test"]
            kind = job["kind"]
            indices = job["indices"]
            expected = job["expected"]
            try:
                if kind == "opt_tpch":
                    sqls = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
                    if len(sqls) != job["size"]:
                        raise RuntimeError("size changed")
                    diff = []
                    for i in indices:
                        _, sql, opt = sqls[i]
                        a = transpile(sql, write="duckdb")[0]
                        b = transpile(opt, write="duckdb")[0]
                        fp = hashlib.blake2b(json.dumps([a, b]).encode(), digest_size=16).hexdigest()
                        if fp != expected[str(i)]:
                            diff.append(i)
                    if not diff:
                        print(json.dumps({"test": test_id, "phase1": "pass"}), file=out, flush=True)
                    else:
                        print(json.dumps({"test": test_id, "phase1": "diff", "py_diff": diff, "dk_only": []}), file=out, flush=True)
                    continue

                schema_dict = TPCDS_SCHEMA if kind == "tpcds" else TPCH_SCHEMA
                rel = "optimizer/tpc-ds/tpc-ds.sql" if kind == "tpcds" else "optimizer/tpc-h/tpc-h.sql"
                sqls = list(load_sql_fixture_pairs(rel))
                if len(sqls) != job["size"]:
                    raise RuntimeError("size changed")
                empty_tables = {tbl: Table(columns=cols, rows=[]) for tbl, cols in schema_dict.items()}
                schema = ensure_schema(schema_dict)
                py_diff, dk_only = [], []
                diff_budget = job.get("diff_budget")
                costs = job.get("costs")
                opt_only = bool(job.get("opt_only")) and kind == "tpcds"
                diff_cost = 0
                aborted = False
                for i in indices:
                    meta, sql, _ = sqls[i]
                    exp_i = expected[str(i)]
                    expr = pickle.loads(pickle.dumps(parse_one(sql)))
                    if not meta.get("execute"):
                        if exp_i["exec"]:
                            py_diff.append(i)
                        continue
                    if not exp_i["exec"]:
                        py_diff.append(i)
                        continue
                    tnames = sorted({t.name for t in find_tables(expr)})
                    if opt_only and "opt_fp" in exp_i:
                        optimized = optimize(expr, schema, leave_tables_isolated=True)
                        ho = hashlib.blake2b(digest_size=16)
                        ho.update(json.dumps(tnames).encode())
                        _ast_fp(optimized, ho)
                        if ho.hexdigest() != exp_i["opt_fp"]:
                            py_diff.append(i)
                            if costs:
                                diff_cost += costs[i]
                        if diff_budget is not None and diff_cost > diff_budget:
                            aborted = True
                            break
                        continue
                    tables = ensure_tables({name: empty_tables[name] for name in tnames})
                    if tables.supported_table_args and tables.supported_table_args != schema.supported_table_args:
                        raise RuntimeError("supported_table_args mismatch")
                    optimized = optimize(expr, schema, leave_tables_isolated=True)
                    optimized.sql(pretty=True)
                    plan = Plan(optimized)
                    str(plan)
                    pe = PythonExecutor(tables=tables)
                    gen_log = []
                    orig_gen = pe.generator.generate
                    def logged_gen(e, _orig=orig_gen, _log=gen_log):
                        s = _orig(e)
                        _log.append(s)
                        return s
                    pe.generator.generate = logged_gen
                    result = pe.execute(plan)
                    seen_sq = set()
                    while True:
                        todo = sorted(k for k in pe._subquery_plans if k not in seen_sq)
                        if not todo:
                            break
                        for k in todo:
                            seen_sq.add(k)
                            subplan, _ = pe._subquery_plans[k]
                            pe.execute(subplan)
                    rt = pickle.loads(pickle.dumps(result))
                    h = hashlib.blake2b(digest_size=16)
                    h.update(json.dumps([tnames, list(rt.columns), sorted(gen_log)]).encode())
                    _ast_fp(optimized, h)
                    py_fp = h.hexdigest()
                    duck_sql = transpile(sql, write="duckdb")[0]
                    dk_fp = hashlib.blake2b(duck_sql.encode(), digest_size=16).hexdigest()
                    if py_fp != exp_i["py_fp"]:
                        py_diff.append(i)
                        if costs:
                            diff_cost += costs[i]
                    elif dk_fp != exp_i["dk_fp"]:
                        dk_only.append(i)
                        if costs:
                            diff_cost += costs[i]
                    if diff_budget is not None and diff_cost > diff_budget:
                        aborted = True
                        break
                if aborted:
                    print(json.dumps({"test": test_id, "phase1": "fail"}), file=out, flush=True)
                elif not py_diff and not dk_only:
                    print(json.dumps({"test": test_id, "phase1": "pass"}), file=out, flush=True)
                else:
                    print(json.dumps({"test": test_id, "phase1": "diff", "py_diff": py_diff, "dk_only": dk_only}), file=out, flush=True)
            except BaseException:
                print(json.dumps({"test": test_id, "phase1": "fail"}), file=out, flush=True)
        return
    if sys.argv[-1] == "tpc_csv":
        LAZY = True
        try:
            module = importlib.import_module("tests.test_executor")
            base = module.TestExecutor
            suite = values["suite"]
            table = values["table"]
            indices = values.get("indices", [])
            needed_tables = values.get("tables")
            orig_tpch, orig_tpcds = module.TPCH_SCHEMA, module.TPCDS_SCHEMA
            # 1. Verify setUpClass loads this modified CSV cleanly into both DuckDB and Table.
            try:
                if suite == "tpch":
                    module.TPCDS_SCHEMA = {}
                    module.TPCH_SCHEMA = {table: orig_tpch[table]}
                else:
                    module.TPCH_SCHEMA = {}
                    module.TPCDS_SCHEMA = {table: orig_tpcds[table]}
                base.setUpClass()
                base.tearDownClass()
            except BaseException:
                print(json.dumps({"setup_ok": False}), file=out, flush=True)
                return
            finally:
                module.TPCH_SCHEMA, module.TPCDS_SCHEMA = orig_tpch, orig_tpcds

            # 2. Run only the affected tests (or sliced queries for TPC-DS).
            if suite == "tpch":
                class OnlyTPCH(base):
                    @classmethod
                    def setUpClass(cls):
                        module.TPCDS_SCHEMA = {}
                        try:
                            super().setUpClass()
                        finally:
                            module.TPCDS_SCHEMA = orig_tpcds
                failed = []
                res = unittest.TestResult()
                cases = {OnlyTPCH(m): f"tests/test_executor.py::TestExecutor::{m}"
                         for m in ("test_execute_tpch", "test_optimized_tpch")}
                unittest.TestSuite(list(cases)).run(res)
                if any(t not in cases for t, _ in res.errors):
                    failed = list(cases.values())
                else:
                    bad = {t for t, _ in res.errors + res.failures}
                    failed = [cases[t] for t in bad if t in cases]
                print(json.dumps({"setup_ok": True, "failed": failed}), file=out, flush=True)
            else:
                if not indices or needed_tables is None:
                    print(json.dumps({"setup_ok": True, "tpcds_ok": False}), file=out, flush=True)
                    return
                class SlicedCSV(base):
                    @classmethod
                    def setUpClass(cls):
                        module.TPCH_SCHEMA = {}
                        want = set(needed_tables)
                        module.TPCDS_SCHEMA = {k: v for k, v in orig_tpcds.items() if k in want}
                        try:
                            super().setUpClass()
                        finally:
                            module.TPCH_SCHEMA, module.TPCDS_SCHEMA = orig_tpch, orig_tpcds
                        cls.tpcds_sqls = [cls.tpcds_sqls[i] for i in indices]
                res = unittest.TestResult()
                res.failfast = True
                unittest.TestSuite([SlicedCSV("test_execute_tpcds")]).run(res)
                ok = bool(res.testsRun == 1 and not res.errors and not res.failures
                          and not res.skipped and not res.unexpectedSuccesses)
                print(json.dumps({"setup_ok": True, "tpcds_ok": ok}), file=out, flush=True)
        except BaseException:
            print(json.dumps({"setup_ok": False}), file=out, flush=True)
        return
    if sys.argv[-1] == "slices":
        # One fixture-driven test, with its loaded list cut down to the given elements.
        LAZY = True
        verdict = None
        try:
            filename, classname, method = values["test"].split("::")
            module = importlib.import_module(filename[:-3].replace("/", "."))
            base = getattr(module, classname)
            attribute, indices = values["attr"], values["indices"]
            setup_safe = bool(values.get("setup_safe"))
            needed_tables = values.get("tables")
            tpch_only = bool(values.get("tpch_only"))

            class Sliced(base):
                @classmethod
                def setUpClass(cls):
                    if setup_safe and filename == "tests/test_executor.py" and classname == "TestExecutor":
                        orig_tpch, orig_tpcds = module.TPCH_SCHEMA, module.TPCDS_SCHEMA
                        try:
                            if needed_tables is not None:
                                module.TPCH_SCHEMA = {}
                                want = set(needed_tables)
                                module.TPCDS_SCHEMA = {k: v for k, v in orig_tpcds.items() if k in want}
                            elif tpch_only:
                                module.TPCDS_SCHEMA = {}
                            super().setUpClass()
                        finally:
                            module.TPCH_SCHEMA, module.TPCDS_SCHEMA = orig_tpch, orig_tpcds
                    else:
                        super().setUpClass()
                    loaded = getattr(cls, attribute)
                    if len(loaded) != values["size"]:
                        raise RuntimeError("fixture list changed length")
                    setattr(cls, attribute, [loaded[i] for i in indices])

            result = unittest.TestResult()
            result.failfast = True
            unittest.TestSuite([Sliced(method)]).run(result)
            verdict = bool(result.testsRun == 1 and not result.errors and not result.failures
                           and not result.skipped and not result.unexpectedSuccesses)
        except BaseException:
            verdict = False
        print(json.dumps({"slices": verdict}), file=out, flush=True)
        return
    skip_executor_setup = False
    if isinstance(values, dict):
        skip_executor_setup = bool(values.get("skip_executor_setup"))
        values = values.get("ids", [])
    for node in values:
        parts = node.split("::")
        if len(parts) == 3 and parts[0].endswith(".py"):
            groups[(parts[0], parts[1])].append((parts[2], node))

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.passed = []
            self.broke = None

        def addSuccess(self, test):
            super().addSuccess(test)
            self.passed.append(test)

        def stopTest(self, test):
            super().stopTest(test)
            # A worker that dies leaves a class-level pool unusable for every later test.
            pool = getattr(type(test), "pool", None)
            if self.broke is None and pool is not None and getattr(pool, "_broken", False):
                self.broke = test

    finished = 0
    for (filename, classname), methods in groups.items():
        modname = filename[:-3].replace("/", ".")
        try:
            module = importlib.import_module(modname)
            cls = getattr(module, classname)
            if not issubclass(cls, unittest.TestCase):
                continue
            if hasattr(module, "pytestmark") or hasattr(cls, "pytestmark"):
                continue
            cases = {
                cls(method): node
                for method, node in methods
                if method.isidentifier()
                and hasattr(cls, method)
                and not hasattr(getattr(cls, method), "pytestmark")
            }
            if not cases:
                continue
            if (skip_executor_setup and filename == "tests/test_executor.py" and classname == "TestExecutor"
                    and not any(m in {"test_execute_tpcds", "test_execute_tpch", "test_optimized_tpch"} for m, _ in methods)):
                cls.setUpClass = classmethod(lambda c: None)
                cls.tearDownClass = classmethod(lambda c: None)
            result = Result()
            unittest.TestSuite(list(cases)).run(result)
            # unittest represents module/class fixture errors with a synthetic test object.
            if any(test not in cases for test, _ in result.errors):
                continue
            if result.broke is not None and result.broke in cases:
                print(json.dumps({"broken": cases[result.broke]}), file=out, flush=True)
            print(
                json.dumps([cases[test] for test in result.passed]),
                file=out,
                flush=True,
            )
            finished += 1
        except Exception:
            continue
    print(json.dumps({"done": finished}), file=out, flush=True)


if __name__ == "__main__":
    _run()
