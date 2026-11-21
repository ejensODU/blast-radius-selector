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
            destination.write_text(descendants[""], encoding="utf-8")
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


def probe(
    root: Path, changes: dict[str, str], ids: list[str], timeout: float
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
    for record in _invoke(root, changes, ids, timeout, "tests"):
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
