"""Candidates for edits outside plain function bodies, from an import-state diff.

The patched sqlglot is imported once in a fresh interpreter and every module
global and class attribute is fingerprinted (worldfp.py). Cells that differ from
the pinned commit's snapshot are the whole effect of a module-level or
class-level edit, including anything derived from it at import time. Each
changed cell is then mapped to the tests that read it, using maps recorded from
the pinned commit: per-key reads of dicts, attribute-name loads, line coverage of
functions, and the modules each test imports.
"""
from __future__ import annotations

import ast
import gzip
import os
import pickle
import signal
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

APP = Path(__file__).resolve().parent
LAZY_LOAD = "tests/dialects/test_dialect.py::TestDialect::test_lazy_load"


@lru_cache(maxsize=1)
def _data():
    with gzip.open(APP / "world.pkl.gz", "rb") as f:
        world = pickle.load(f)
    with gzip.open(APP / "keyreads.pkl.gz", "rb") as f:
        keys = pickle.load(f)
    return world, keys


def _module_name(path: str) -> str:
    name = path[:-3].replace("/", ".")
    return name.removesuffix(".__init__")


def _snapshot(root: Path, changes: dict[str, str], names: list[str], timeout: float):
    from probe import _overlay
    try:
        with tempfile.TemporaryDirectory(prefix="world-") as directory:
            shadow = Path(directory, "tree")
            shadow.mkdir()
            _overlay(root.resolve(), shadow, changes)
            out = Path(directory, "snapshot.pkl")
            child = subprocess.Popen(
                [sys.executable, "-B", str(APP / "worldfp.py"), "--names", str(shadow), str(out)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(shadow), SKIP_INTEGRATION="0"))
            try:
                child.communicate("\n".join(names).encode(), timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.communicate()
                return None
            if child.returncode != 0 or not out.exists():
                return None
            with out.open("rb") as f:
                return pickle.load(f)
    except (OSError, ValueError, pickle.PickleError, EOFError):
        return None


def _spans(source: str):
    """qualified name -> (first line, last line) for every def in the file."""
    out = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([child.lineno] + [d.lineno for d in child.decorator_list])
                out[prefix + child.name] = (first, child.end_lineno)
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")

    try:
        walk(ast.parse(source), "")
    except SyntaxError:
        pass
    return out


def candidates(root: Path, index, changes: dict[str, str], timeout: float = 20.0):
    """A test mask, or None when this analysis cannot vouch for the patch."""
    world, keys = _data()
    profile = index.profile
    if keys["ids"] != profile["ids"]:
        return None
    paths = {cell[0] for cell in world["cells"]}
    if any(path not in paths for path in changes):
        return None  # a file the snapshot does not cover (new, or not importable by name)
    # Only modules that load a changed module while they import can hold state derived from it.
    scope = set(changes)
    for path in changes:
        scope.update(world["importers"].get(path, ()))
    names = sorted({_module_name(path) for path in scope})
    snap = _snapshot(root, changes, names, timeout)
    if snap is None:
        return None
    line_maps, name_masks, classes = profile["lines"], profile["names"], profile["classes"]
    imports, reads, wrapped, alias = keys["imports"], keys["reads"], keys["wrapped"], keys["alias"]
    mask = 0

    failed_paths = set()
    for name in snap["failed"]:
        for path in (name.replace(".", "/") + ".py", name.replace(".", "/") + "/__init__.py"):
            if path in paths:
                failed_paths.add(path)
    for path in failed_paths:
        if path in world["eager"]:
            return None  # the package itself no longer imports: everything is affected
        mask |= imports.get(path, 0)

    old, new = world["cells"], snap["cells"]
    old_parts, new_parts = world["parts"], snap["parts"]
    touched = set()
    spans = {}
    for cell in set(old) | set(new):
        path, qual, attr = cell
        if path not in scope or path in failed_paths or cell in world["unstable"] or old.get(cell) == new.get(cell):
            continue
        touched.add(path)
        if attr == "<bases>":
            return None
        target = alias.get(cell, cell)
        row = reads.get(target, {})
        attr_mask = name_masks.get(attr, 0)
        if qual:
            owners = classes.get((path, qual))
            if owners is not None:
                attr_mask &= owners | name_masks.get(qual.rpartition(".")[2], 0)
        if target in wrapped and cell in old_parts and cell in new_parts:
            before, after = old_parts[cell], new_parts[cell]
            mask |= row.get("*", 0)
            for tok in set(before) | set(after):
                if before.get(tok) != after.get(tok):
                    mask |= row.get(tok, 0)
            continue
        if target in wrapped:
            for value in row.values():
                mask |= value
            mask |= attr_mask
            continue
        if path not in spans:
            try:
                spans[path] = _spans((root / path).read_text(encoding="utf-8"))
            except OSError:
                spans[path] = {}
        span = spans[path].get(f"{qual}.{attr}" if qual else attr)
        if span is not None and cell in old and cell in new:
            hits = line_maps.get(path, {})
            for line in range(span[0], span[1] + 1):
                mask |= hits.get(line, 0)
            continue
        mask |= attr_mask
    for path in touched | failed_paths:
        mask |= keys["untracked"] & imports.get(path, -1)
    if (touched | failed_paths | set(changes)) & set(world["eager"]):
        if LAZY_LOAD in index.row and LAZY_LOAD in profile["ids"]:
            mask |= 1 << profile["ids"].index(LAZY_LOAD)
    return mask
