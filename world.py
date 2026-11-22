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

LAST_CHANGED: list[tuple[str, str, str]] = []
LAST_FAILED: list[str] = []
LAST_PURE_BODY: bool = False
LAST_FN_SPANS: list[tuple[str, tuple[int, int]]] | None = None


@lru_cache(maxsize=1)
def _data():
    with gzip.open(APP / "world.pkl.gz", "rb") as f:
        world = pickle.load(f)
    with gzip.open(APP / "keyreads.pkl.gz", "rb") as f:
        keys = pickle.load(f)
    return world, keys


@lru_cache(maxsize=1)
def _dispatch_maps():
    _, keys = _data()
    by_tok: dict[str, int] = {}
    by_lower: dict[str, int] = {}
    for cell, row in keys.get("reads", {}).items():
        if isinstance(cell, tuple) and len(cell) == 3 and cell[0] == "<derived>" and cell[2] == "dispatch":
            for tok, m in row.items():
                by_tok[tok] = by_tok.get(tok, 0) | m
                if isinstance(tok, str) and tok.startswith("c:"):
                    short = tok.rsplit(".", 1)[-1].lower()
                    by_lower[short] = by_lower.get(short, 0) | m
    return by_tok, by_lower


@lru_cache(maxsize=2)
def _test_imports(root_str: str):
    """Map (source_path, attr) -> set of test file relative paths doing `from mod import attr`."""
    root = Path(root_str)
    tests_dir = root / "tests"
    attr_imp: dict[tuple[str, str], set[str]] = {}
    star_imp: dict[str, set[str]] = {}
    if not tests_dir.is_dir():
        return attr_imp, star_imp
    for path in tests_dir.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                base = node.module.replace(".", "/")
                cands = (base + ".py", base + "/__init__.py")
                for alias in node.names:
                    for cand in cands:
                        if alias.name == "*":
                            star_imp.setdefault(cand, set()).add(rel)
                        else:
                            attr_imp.setdefault((cand, alias.name), set()).add(rel)
    return attr_imp, star_imp


def _import_break_mask(path: str, attr: str, index, root: Path) -> int:
    attr_imp, star_imp = _test_imports(str(root.resolve()))
    tfiles = attr_imp.get((path, attr), set()) | star_imp.get(path, set())
    if not tfiles:
        return 0
    line_maps = index.profile["lines"]
    reads = index.profile["reads"]
    mask = 0
    for tf in tfiles:
        if tf in {"tests/helpers.py", "tests/dialects/test_dialect.py", "tests/__init__.py"}:
            return index.full
        m = index.by_file.get(tf, 0) | reads.get(tf, 0)
        for lmask in line_maps.get(tf, {}).values():
            m |= lmask
        mask |= m or index.full
    return mask


def _module_name(path: str) -> str:
    name = path[:-3].replace("/", ".")
    return name.removesuffix(".__init__")


def _snapshot(root: Path, changes: dict[str, str], names: list[str], timeout: float):
    from probe import _overlay, probe_world
    snap = probe_world(root, changes, names, timeout)
    if snap is not None:
        return snap
    try:
        with tempfile.TemporaryDirectory(prefix="world-") as directory:
            shadow = Path(directory, "tree")
            shadow.mkdir()
            _overlay(root.resolve(), shadow, changes)
            out = Path(directory, "snapshot.pkl")
            child = subprocess.Popen(
                [sys.executable, "-B", str(APP / "worldfp.py"), "--names", str(shadow), str(out)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(shadow), SKIP_INTEGRATION="0",
                         PYTHONHASHSEED="0"))
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


def candidates(
    root: Path,
    index,
    changes: dict[str, str],
    timeout: float = 20.0,
    body_cells: set[tuple[str, str, str]] | None = None,
    allow_new: bool = False,
):
    """A test mask, or None when this analysis cannot vouch for the patch."""
    global LAST_PURE_BODY, LAST_FN_SPANS
    LAST_CHANGED.clear()
    LAST_FAILED.clear()
    LAST_PURE_BODY = False
    LAST_FN_SPANS = None
    world, keys = _data()
    dispatch_by_tok, dispatch_by_lower = _dispatch_maps()
    profile = index.profile
    if keys["ids"] != profile["ids"]:
        return None
    paths = {cell[0] for cell in world["cells"]}
    new_paths = {path for path in changes if path not in paths}
    if new_paths and not allow_new:
        return None  # a file the snapshot does not cover (new, or not importable by name)
    # Only modules that load a changed module while they import can hold state derived from it.
    if new_paths:
        scope = set(paths) | set(changes)
    else:
        scope = set(changes)
        for path in changes:
            scope.update(world["importers"].get(path, ()))
    names = sorted({_module_name(path) for path in scope})
    snap = _snapshot(root, changes, names, timeout)
    if snap is None or snap.get("seed") != world.get("seed"):
        return None  # fingerprints from a different interpreter build cannot be compared
    line_maps, name_masks, classes = profile["lines"], profile["names"], profile["classes"]
    imports, reads, wrapped, alias = keys["imports"], keys["reads"], keys["wrapped"], keys["alias"]
    attr_reads, attr_wrapped = keys.get("attr_reads", {}), keys.get("attr_wrapped", ())
    mask = 0

    failed_paths = set()
    for name in snap["failed"]:
        for path in (name.replace(".", "/") + ".py", name.replace(".", "/") + "/__init__.py"):
            if path in paths or path in new_paths:
                failed_paths.add(path)
    LAST_FAILED.extend(sorted(failed_paths))
    for path in failed_paths:
        if path in world["eager"] or path in new_paths:
            return None  # the package itself no longer imports: everything is affected
        mask |= imports.get(path, 0)

    old, new = world["cells"], snap["cells"]
    old_parts, new_parts = world["parts"], snap["parts"]
    touched = set()
    spans = {}
    derived = False
    fn_spans: list[tuple[str, tuple[int, int]]] = []
    all_fn_spans = True
    for cell in set(old) | set(new):
        path, qual, attr = cell
        if path not in scope or path in failed_paths or path in new_paths:
            continue
        if cell in world["unstable"]:
            if path in changes:
                derived = True
                all_fn_spans = False
                LAST_CHANGED.append(cell)
                touched.add(path)
                mod_imp = imports.get(path, 0)
                mask |= (name_masks.get(attr, 0) & mod_imp) or mod_imp
            continue
        if old.get(cell) == new.get(cell):
            continue
        if body_cells and cell in body_cells and cell in old and cell in new:
            continue  # plain function-body edit handled by line coverage + operand refinement
        derived = True
        LAST_CHANGED.append(cell)
        touched.add(path)
        if attr == "<bases>":
            return None
        if cell in old and cell not in new and not qual:
            mask |= _import_break_mask(path, attr, index, root)
        target = alias.get(cell, cell)
        row = reads.get(target, {})
        mod_imp = imports.get(path, 0)
        attr_mask = name_masks.get(attr, 0) & mod_imp
        if qual:
            owners = classes.get((path, qual))
            if owners is not None:
                attr_mask &= owners | name_masks.get(qual.rpartition(".")[2], 0)
        if target in wrapped and cell in old_parts and cell in new_parts:
            all_fn_spans = False
            before, after = old_parts[cell], new_parts[cell]
            mask |= row.get("*", 0)
            for tok in set(before) | set(after):
                if before.get(tok) != after.get(tok):
                    mask |= row.get(tok, 0)
                    if attr == "TRANSFORMS":
                        mask |= dispatch_by_tok.get(tok, 0) & mod_imp
            continue
        if target in wrapped:
            all_fn_spans = False
            for value in row.values():
                mask |= value
            mask |= attr_mask
            continue
        if cell in old and cell in attr_wrapped:
            all_fn_spans = False
            mask |= attr_reads.get(cell, 0)
            continue
        if path not in spans:
            try:
                spans[path] = _spans((root / path).read_text(encoding="utf-8"))
            except OSError:
                spans[path] = {}
        span = spans[path].get(f"{qual}.{attr}" if qual else attr)
        if qual and attr.endswith("_sql") and (cell not in old or cell not in new or span is None):
            dmask = dispatch_by_lower.get(attr[:-4].lower(), 0)
            mask |= (dmask & mod_imp) if dmask else mod_imp
        if span is not None and cell in old:
            hits = line_maps.get(path, {})
            for line in range(span[0], span[1] + 1):
                mask |= hits.get(line, 0)
            if cell in new:
                fn_spans.append((path, span))
                continue
        all_fn_spans = False
        mask |= attr_mask
    if derived and all_fn_spans and fn_spans and not failed_paths:
        LAST_FN_SPANS = fn_spans
    if body_cells and not derived and not failed_paths:
        LAST_PURE_BODY = True
    for path in touched | failed_paths:
        mask |= keys["untracked"] & imports.get(path, -1)
    if (touched | failed_paths | (set(changes) if derived or failed_paths else set())) & set(world["eager"]):
        if LAZY_LOAD in index.row and LAZY_LOAD in profile["ids"]:
            mask |= 1 << profile["ids"].index(LAZY_LOAD)
    return mask
