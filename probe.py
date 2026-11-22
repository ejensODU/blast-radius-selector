"""Run a small batch of unittest cases against one temporary, patched checkout."""

from __future__ import annotations

import ast
import atexit
import json
import os
import pickle
import select
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path


# Tests after which a class-level process pool was found broken, from the last probe.
LAZY = False
BROKEN: list[str] = []
# Non-empty when the last probe ran every requested class to the end.
COMPLETE: list[bool] = []

_ZYGOTE: subprocess.Popen | None = None
_ZYGOTE_FAILED = False
_WORLD_ZYGOTE: subprocess.Popen | None = None
_WORLD_ZYGOTE_FAILED = False
_LIVE_ZYGOTE_CPU: dict[str, float] = {"test": 0.0, "world": 0.0}


def _stop_proc(proc: subprocess.Popen | None, key: str | None = None) -> None:
    if key is not None:
        _LIVE_ZYGOTE_CPU[key] = 0.0
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=0.5)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass


def _stop_zygote() -> None:
    global _ZYGOTE, _WORLD_ZYGOTE
    p1, _ZYGOTE = _ZYGOTE, None
    p2, _WORLD_ZYGOTE = _WORLD_ZYGOTE, None
    _stop_proc(p1, "test")
    _stop_proc(p2, "world")


atexit.register(_stop_zygote)


def _spawn_zygote(kind: str) -> subprocess.Popen | None:
    env = dict(
        os.environ,
        PYTHONHASHSEED="0",
        SKIP_INTEGRATION="0",
        PYTHONDONTWRITEBYTECODE="1",
    )
    proc = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).resolve()), kind],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    ready, _, _ = select.select([proc.stdout], [], [], 10.0)
    if not ready:
        _stop_proc(proc)
        return None
    line = proc.stdout.readline()
    if line.strip() != b"READY":
        _stop_proc(proc)
        return None
    return proc


def _get_zygote(kind: str = "test") -> subprocess.Popen | None:
    global _ZYGOTE, _ZYGOTE_FAILED, _WORLD_ZYGOTE, _WORLD_ZYGOTE_FAILED
    if not hasattr(os, "fork"):
        return None
    if kind == "world":
        if _WORLD_ZYGOTE_FAILED:
            return None
        if _WORLD_ZYGOTE is not None and _WORLD_ZYGOTE.poll() is None:
            return _WORLD_ZYGOTE
        try:
            proc = _spawn_zygote("world_zygote")
            if proc is None:
                _WORLD_ZYGOTE_FAILED = True
                return None
            _WORLD_ZYGOTE = proc
            return proc
        except Exception:
            _WORLD_ZYGOTE_FAILED = True
            return None
    if _ZYGOTE_FAILED:
        return None
    if _ZYGOTE is not None and _ZYGOTE.poll() is None:
        return _ZYGOTE
    try:
        proc = _spawn_zygote("zygote")
        if proc is None:
            _ZYGOTE_FAILED = True
            return None
        _ZYGOTE = proc
        return proc
    except Exception:
        _ZYGOTE_FAILED = True
        return None


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


def _readline_timeout(stream, deadline: float) -> bytes | None:
    rem = deadline - time.monotonic()
    if rem <= 0:
        return None
    ready, _, _ = select.select([stream], [], [], rem)
    if not ready:
        return None
    line = stream.readline()
    return line if line else None


def _invoke_zygote(shadow: Path, out_path: Path, values, timeout: float, mode: str, heavy: bool):
    """Run mode in a forked child of the pre-warmed PYTHONHASHSEED=0 zygote. Returns True if handled."""
    zkind = "world" if mode == "world" else "test"
    proc = _get_zygote(kind=zkind)
    if proc is None or proc.stdin is None or proc.stdout is None:
        return False
    req = {"cwd": str(shadow), "out": str(out_path), "mode": mode, "values": values, "heavy": heavy}
    try:
        proc.stdin.write((json.dumps(req) + "\n").encode())
        proc.stdin.flush()
    except OSError:
        _stop_proc(proc, zkind)
        return False
    deadline = time.monotonic() + timeout
    # First line reports child PID (may take ~0.5s on first heavy=True warmup).
    pid_line = _readline_timeout(proc.stdout, deadline + (5.0 if heavy else 1.0))
    if not pid_line:
        _stop_proc(proc, zkind)
        return False
    try:
        pid = json.loads(pid_line).get("pid")
    except Exception:
        _stop_proc(proc, zkind)
        return False
    if not isinstance(pid, int) or pid <= 0:
        _stop_proc(proc, zkind)
        return False
    done_line = _readline_timeout(proc.stdout, deadline)
    if done_line is None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        # Wait briefly for zygote to reap child and report done.
        done_line = _readline_timeout(proc.stdout, time.monotonic() + 2.0)
        if done_line is None:
            _stop_proc(proc, zkind)
    if done_line:
        try:
            cpu_val = json.loads(done_line).get("cpu")
            if isinstance(cpu_val, (int, float)):
                _LIVE_ZYGOTE_CPU[zkind] = float(cpu_val)
        except Exception:
            pass
    return True


def _invoke(root, changes, values, timeout, mode, heavy=False):
    """Run the requested check with a hard wall clock budget and a private result channel."""
    if not values or not changes or timeout <= 0:
        return []
    if any(
        Path(p).is_absolute() or set(p.split("/")) & {"", ".", ".."} for p in changes
    ):
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="blast-") as directory:
            base = Path(directory)
            shadow = base / "src"
            shadow.mkdir()
            out_path = base / "out.jsonl"
            _overlay(root.resolve(), shadow, changes)
            if _invoke_zygote(shadow, out_path, values, timeout, mode, heavy):
                raw = out_path.read_bytes() if out_path.exists() else b""
            else:
                child = subprocess.Popen(
                    [sys.executable, "-B", str(Path(__file__).resolve()), mode],
                    cwd=shadow,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=dict(
                        os.environ,
                        PYTHONHASHSEED="0",
                        SKIP_INTEGRATION="0",
                        PYTHONDONTWRITEBYTECODE="1",
                        PYTHONPATH=os.pathsep.join(
                            [str(shadow), os.environ.get("PYTHONPATH", "")]
                        ),
                    ),
                )
                try:
                    raw, _ = child.communicate(
                        json.dumps(values).encode(), timeout=timeout
                    )
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    raw, _ = child.communicate()
            records = []
            for line in raw.splitlines():
                try:
                    records.append(json.loads(line))
                except (UnicodeError, ValueError, TypeError):
                    continue
            return records
    except (OSError, ValueError):
        return []


def probe_world(root: Path, changes: dict[str, str], names: list[str], timeout: float = 20.0):
    """Run worldfp import-state snapshot inside a forked child of the PYTHONHASHSEED=0 zygote."""
    if not names or not changes or timeout <= 0:
        return None
    if any(Path(p).is_absolute() or set(p.split("/")) & {"", ".", ".."} for p in changes):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="world-") as directory:
            base = Path(directory)
            shadow = base / "src"
            shadow.mkdir()
            out_path = base / "snap.pkl"
            _overlay(root.resolve(), shadow, changes)
            if _invoke_zygote(shadow, out_path, {"names": names, "dest": str(out_path)}, timeout, "world", heavy=False):
                if out_path.is_file():
                    return pickle.loads(out_path.read_bytes())
                return None
            return None
    except Exception:
        return None


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
    for record in _invoke(root, changes, spec, timeout, "slices", heavy=True):
        if isinstance(record, dict) and "slices" in record:
            return record["slices"]
    return None


def probe_opt_slices(root: Path, changes: dict[str, str], spec: dict, timeout: float):
    """Run a sliced fail-fast TestOptimizer check_file test. Returns (verdict, broken)."""
    for record in _invoke(root, changes, spec, timeout, "opt_slices", heavy=True):
        if isinstance(record, dict) and "opt_slices" in record:
            return record["opt_slices"], bool(record.get("broken"))
    return None, False


def probe_phase1(root: Path, changes: dict[str, str], specs: list[dict], timeout: float) -> dict[str, dict]:
    """Run deterministic Phase-1 dry-run checks for executor TPC tests in one fresh interpreter."""
    out = {}
    for record in _invoke(root, changes, specs, timeout, "phase1", heavy=False):
        if isinstance(record, dict) and "test" in record and "phase1" in record:
            out[record["test"]] = record
    return out


def probe_tpc_csv(root: Path, changes: dict[str, bytes], spec: dict, timeout: float) -> dict | None:
    """Validate a binary TPC .csv.gz edit against TestExecutor setup and the affected queries."""
    for record in _invoke(root, changes, spec, timeout, "tpc_csv", heavy=True):
        if isinstance(record, dict) and "setup_ok" in record:
            return record
    return None


def probe(
    root: Path,
    changes: dict[str, str],
    ids: list[str],
    timeout: float,
    skip_executor_setup: bool = False,
    failfast_subtest: bool = False,
    fixture_slices: dict[str, dict] | None = None,
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
    files = {g[0] for g in groups}
    heavy = bool(files & {"tests/test_optimizer.py", "tests/test_executor.py"})
    payload = {
        "ids": ids,
        "skip_executor_setup": skip_executor_setup,
        "failfast_subtest": failfast_subtest,
    }
    if fixture_slices:
        payload["fixture_slices"] = fixture_slices
    for record in _invoke(root, changes, payload, timeout, "tests", heavy=heavy):
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
    records = _invoke(root, changes, sorted(set(modules)), timeout, "imports", heavy=False)
    return records[-1] if records and isinstance(records[-1], bool) else None


def _install_pool() -> None:
    """Run process-pool tasks in this process, keeping the pickle boundaries.

    A real pool only adds what the boundaries add: arguments are unpickled in a
    worker, results and exceptions are unpickled in the parent, and a worker that
    dies while unpickling leaves the pool broken for every later caller.
    """
    import concurrent.futures as futures
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

    orig_wait = futures.wait
    orig_as_completed = futures.as_completed
    done_not_done = getattr(futures._base, "DoneAndNotDoneFutures", lambda d, n: (d, n))

    def _wait(fs, timeout=None, return_when=futures.ALL_COMPLETED):
        fs_list = list(fs)
        if LAZY and any(isinstance(f, Deferred) for f in fs_list):
            return done_not_done(set(fs_list), set())
        return orig_wait(fs_list, timeout=timeout, return_when=return_when)

    def _as_completed(fs, timeout=None):
        fs_list = list(fs)
        if LAZY and any(isinstance(f, Deferred) for f in fs_list):
            for f in fs_list:
                yield f
            return
        yield from orig_as_completed(fs_list, timeout=timeout)

    futures.ProcessPoolExecutor = Pool
    futures.wait = _wait
    futures.as_completed = _as_completed


class _StopSub(BaseException):
    pass


def _execute_mode(mode: str, values, out) -> None:
    global LAZY

    if mode == "world":
        import worldfp
        names = values["names"]
        dest = values["dest"]
        root = os.getcwd()
        failed = worldfp.load(names)
        cells, parts = worldfp.snapshot(root, set(names))
        data = {"cells": cells, "parts": parts, "failed": failed, "seed": worldfp._h(*worldfp.SEED_CHECK)}
        with open(dest, "wb") as fh:
            pickle.dump(data, fh, protocol=5)
        return

    import importlib
    import unittest

    _install_pool()
    groups = defaultdict(list)

    if mode == "imports":
        try:
            for module_name in values:
                importlib.import_module(module_name)
        except Exception:
            print("false", file=out)
        else:
            print("true", file=out)
        return

    if mode == "phase1":
        import hashlib
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
                empty_tables = {}
                for tbl, cols in schema_dict.items():
                    t = Table(columns=cols, rows=[])
                    t._p1_tag = tbl
                    empty_tables[tbl] = t
                schema = ensure_schema(schema_dict)
                py_diff, dk_only = [], []
                diff_budget = job.get("diff_budget")
                costs = job.get("costs")
                opt_only = bool(job.get("opt_only")) and kind == "tpcds"
                duckdb_only = bool(job.get("duckdb_only")) and kind == "tpcds"
                diff_cost = 0
                aborted = False
                for i in indices:
                    meta, sql, _ = sqls[i]
                    exp_i = expected[str(i)]
                    if duckdb_only and "dk_fp" in exp_i:
                        if not meta.get("execute"):
                            if exp_i["exec"]:
                                py_diff.append(i)
                            continue
                        if not exp_i["exec"]:
                            py_diff.append(i)
                            continue
                        duck_sql = transpile(sql, write="duckdb")[0]
                        dk_fp = hashlib.blake2b(duck_sql.encode(), digest_size=16).hexdigest()
                        if dk_fp != exp_i["dk_fp"]:
                            dk_only.append(i)
                            if costs:
                                diff_cost += costs[i]
                        if diff_budget is not None and diff_cost > diff_budget:
                            aborted = True
                            break
                        continue
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
                    scan_log = []
                    orig_find = tables.find
                    def logged_find(table, *args, _orig=orig_find, _log=scan_log, **kwargs):
                        res = _orig(table, *args, **kwargs)
                        _log.append([str(table), getattr(res, "_p1_tag", None), list(res.columns) if hasattr(res, "columns") else None])
                        return res
                    tables.find = logged_find
                    optimized = optimize(expr, schema, leave_tables_isolated=True)
                    optimized.sql(pretty=True)
                    plan = Plan(optimized)
                    str(plan)
                    dag_sig = sorted(
                        [type(s).__name__, s.name, sorted([type(d).__name__, d.name] for d in deps)]
                        for s, deps in plan.dag.items()
                    )
                    pe = PythonExecutor(tables=tables)
                    gen_log = []
                    orig_gen = pe.generator.generate
                    def logged_gen(e, _orig=orig_gen, _log=gen_log):
                        s = _orig(e)
                        _log.append(s)
                        return s
                    pe.generator.generate = logged_gen
                    step_cols = []
                    orig_table = pe.table
                    def logged_table(expressions, _orig=orig_table, _log=step_cols):
                        t = _orig(expressions)
                        _log.append(list(t.columns))
                        return t
                    pe.table = logged_table
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
                    h.update(json.dumps([tnames, dag_sig, list(rt.columns), sorted(scan_log), sorted(step_cols), sorted(gen_log)]).encode())
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

    if mode == "tpc_csv":
        LAZY = True
        try:
            module = importlib.import_module("tests.test_executor")
            base = module.TestExecutor
            suite = values["suite"]
            table = values["table"]
            indices = values.get("indices", [])
            needed_tables = values.get("tables")
            orig_tpch, orig_tpcds = module.TPCH_SCHEMA, module.TPCDS_SCHEMA
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

    if mode == "opt_slices":
        verdict = False
        broken = False
        try:
            filename, classname, method = values["test"].split("::")
            module = importlib.import_module(filename[:-3].replace("/", "."))
            base = getattr(module, classname)
            rel = values["rel"]
            indices = values["indices"]
            size = values["size"]
            orig_loader = module.load_sql_fixture_pairs
            all_pairs = list(orig_loader(rel))
            if len(all_pairs) != size:
                raise RuntimeError("fixture list changed length")
            selected = [all_pairs[i] for i in indices]

            class SlicedOpt(base):
                def check_file(self, file, func, pretty=False, execute=None, only=None, **kwargs):
                    for i, (meta, sql, expected) in enumerate(selected, start=1):
                        title = meta.get("title") or f"{i}, {sql}"
                        if only and title != only:
                            continue
                        dialect = meta.get("dialect")
                        leave_tables_isolated = meta.get("leave_tables_isolated")
                        validate_qualify_columns = meta.get("validate_qualify_columns")
                        canonicalize_table_aliases = meta.get("canonicalize_table_aliases")
                        func_kwargs = kwargs.copy()
                        if schema := meta.get("schema"):
                            func_kwargs["schema"] = json.loads(schema)
                        if leave_tables_isolated is not None:
                            func_kwargs["leave_tables_isolated"] = module.string_to_bool(leave_tables_isolated)
                        if validate_qualify_columns is not None:
                            func_kwargs["validate_qualify_columns"] = module.string_to_bool(validate_qualify_columns)
                        if dialect:
                            func_kwargs["dialect"] = dialect
                        if canonicalize_table_aliases is not None:
                            func_kwargs["canonicalize_table_aliases"] = module.string_to_bool(canonicalize_table_aliases)
                        fixture_execute = False if execute is False else meta.get("execute", execute)
                        if min_python := meta.get("execute_min_python"):
                            fixture_execute = module.string_to_bool(fixture_execute) and sys.version_info >= tuple(
                                map(int, min_python.split("."))
                            )
                        future = self.pool.submit(module.parse_and_optimize, func, sql, dialect, **func_kwargs)
                        optimized = future.result()
                        actual = optimized.sql(pretty=pretty, dialect=dialect)
                        self.assertEqual(expected, actual)
                        if module.string_to_bool(fixture_execute):
                            dfs = []
                            for expression in (module.parse_one(sql, read=dialect), optimized):
                                if not isinstance(expression, (module.exp.Query, module.exp.Values)):
                                    expression = module.exp.select(module.exp.alias_(expression, "_col_0"))
                                dfs.append(self.conn.execute(expression.sql(dialect="duckdb")).df())
                            module.assert_frame_equal(*dfs)

            res = unittest.TestResult()
            res.failfast = True
            unittest.TestSuite([SlicedOpt(method)]).run(res)
            pool = getattr(SlicedOpt, "pool", None)
            broken = bool(pool is not None and getattr(pool, "_broken", False))
            verdict = bool(
                not broken and res.testsRun == 1 and not res.errors and not res.failures
                and not res.skipped and not res.unexpectedSuccesses
            )
        except BaseException:
            verdict = False
        print(json.dumps({"opt_slices": verdict, "broken": broken}), file=out, flush=True)
        return

    if mode == "slices":
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
    failfast_subtest = False
    fixture_slices = {}
    if isinstance(values, dict):
        skip_executor_setup = bool(values.get("skip_executor_setup"))
        failfast_subtest = bool(values.get("failfast_subtest"))
        fixture_slices = values.get("fixture_slices") or {}
        values = values.get("ids", [])
    LAZY = bool(failfast_subtest)
    for node in values:
        parts = node.split("::")
        if len(parts) == 3 and parts[0].endswith(".py"):
            groups[(parts[0], parts[1])].append((parts[2], node))

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.passed = []
            self.broke = None
            self.aborted = set()

        def addSuccess(self, test):
            if test in self.aborted:
                return
            super().addSuccess(test)
            self.passed.append(test)

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None and failfast_subtest:
                self.aborted.add(test)
                raise _StopSub()

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
            slice_by_method = (
                {m: fixture_slices[node] for m, node in methods if node in fixture_slices}
                if (failfast_subtest and fixture_slices)
                else {}
            )
            if failfast_subtest:
                orig_call = cls._callTestMethod
                def _wrapped_call(self, method, _orig=orig_call, _sbm=slice_by_method, _mod=module):
                    spec = _sbm.get(self._testMethodName)
                    if spec is not None and hasattr(_mod, "load_sql_fixture_pairs"):
                        orig_loader = _mod.load_sql_fixture_pairs
                        rel, idxs, sz = spec["rel"], spec["indices"], spec["size"]
                        def _sliced_loader(f, _ol=orig_loader, _rel=rel, _idxs=idxs, _sz=sz):
                            if f != _rel:
                                return _ol(f)
                            all_pairs = list(_ol(f))
                            if len(all_pairs) != _sz:
                                raise RuntimeError("fixture list changed length")
                            return iter([all_pairs[i] for i in _idxs])
                        _mod.load_sql_fixture_pairs = _sliced_loader
                        try:
                            return _orig(self, method)
                        except _StopSub:
                            return None
                        finally:
                            _mod.load_sql_fixture_pairs = orig_loader
                    try:
                        return _orig(self, method)
                    except _StopSub:
                        return None
                cls._callTestMethod = _wrapped_call
            result = Result()
            unittest.TestSuite(list(cases)).run(result)
            # unittest represents module/class fixture errors with _ErrorHolder,
            # while subTest exceptions use _SubTest with .test_case pointing to the TestCase.
            if any(getattr(test, "test_case", test) not in cases for test, _ in result.errors):
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


def _zygote_loop(kind: str = "test") -> None:
    app_dir = str(Path(__file__).resolve().parent)
    if app_dir not in sys.path:
        sys.path.append(app_dir)
    # Keep protocol channel on private stdout fd; child redirects stdout/stderr to devnull.
    proto_out = os.fdopen(os.dup(1), "w", buffering=1)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_fd, 1)
    os.dup2(devnull_fd, 2)
    os.close(devnull_fd)

    if kind == "world":
        # Must NOT import unittest or logging, which would alter UNITTEST / logger fingerprints!
        import worldfp  # noqa: F401
    else:
        import concurrent.futures  # noqa: F401
        import csv  # noqa: F401
        import enum  # noqa: F401
        import functools  # noqa: F401
        import gzip  # noqa: F401
        import hashlib  # noqa: F401
        import importlib  # noqa: F401
        import re  # noqa: F401
        import types  # noqa: F401
        import unittest  # noqa: F401
        import unittest.mock  # noqa: F401

    heavy_loaded = False
    base_pypath = os.environ.get("PYTHONPATH", "")

    proto_out.write("READY\n")
    proto_out.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        if kind != "world" and req.get("heavy") and not heavy_loaded:
            try:
                import numpy  # noqa: F401
                import pandas  # noqa: F401
                import duckdb  # noqa: F401
            except Exception:
                pass
            heavy_loaded = True
        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                proto_out.close()
                cwd = req["cwd"]
                os.chdir(cwd)
                os.environ["PYTHONPATH"] = os.pathsep.join([cwd, base_pypath])
                sys.path.insert(0, cwd)
                mode = req["mode"]
                if mode == "world":
                    _execute_mode(mode, req["values"], None)
                else:
                    with open(req["out"], "w", buffering=1, encoding="utf-8") as fh:
                        _execute_mode(mode, req["values"], fh)
                        fh.flush()
            except BaseException:
                pass
            finally:
                os._exit(0)
        proto_out.write(json.dumps({"pid": pid}) + "\n")
        proto_out.flush()
        try:
            _, status = os.waitpid(pid, 0)
        except ChildProcessError:
            status = -1
        import resource
        r1, r2 = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
        zcpu = r1.ru_utime + r1.ru_stime + r2.ru_utime + r2.ru_stime
        proto_out.write(json.dumps({"done": status, "cpu": round(zcpu, 4)}) + "\n")
        proto_out.flush()


def _run() -> None:
    if sys.argv[-1] == "zygote":
        _zygote_loop(kind="test")
        return
    if sys.argv[-1] == "world_zygote":
        _zygote_loop(kind="world")
        return
    # Keep application output away from the result channel, including native-library writes.
    out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.path.insert(0, os.getcwd())
    values = json.load(sys.stdin)
    _execute_mode(sys.argv[-1], values, out)


if __name__ == "__main__":
    _run()
