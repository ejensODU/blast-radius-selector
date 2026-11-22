"""Map each independent patch against measured dependencies in the pristine checkout."""
from __future__ import annotations

import argparse
import gzip
import json
import pickle
import sys
import time
from collections import defaultdict
from pathlib import Path

from impact import analyze, apply_file, parse_patch
from schedule import schedule
from symbols import scalar_mask
from values import refine

APP = Path(__file__).resolve().parent

TPC_CDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPC_H = "tests/test_executor.py::TestExecutor::test_execute_tpch"
TPC_OPT_H = "tests/test_executor.py::TestExecutor::test_optimized_tpch"
TPC_SET = frozenset({TPC_CDS, TPC_H, TPC_OPT_H})
TPC_KIND = {TPC_CDS: "tpcds", TPC_H: "tpch", TPC_OPT_H: "opt_tpch"}

_ROW_EXEC_WORLD_FILES = frozenset({
    "sqlglot/executor/__init__.py",
    "sqlglot/executor/context.py",
    "sqlglot/executor/env.py",
    "sqlglot/executor/python.py",
    "sqlglot/executor/table.py",
    "sqlglot/planner.py",
})
_ROW_EXEC_HELPER_ATTRS = frozenset({"is_int", "seq_get", "PYTHON_VERSION", "dict_depth"})
_SHARED_TEST_FILES = frozenset({
    "tests/__init__.py",
    "tests/helpers.py",
    "tests/conftest.py",
    "tests/dialects/__init__.py",
    "tests/dialects/test_dialect.py",
})
_RT_FIXTURES = frozenset({
    "tests/fixtures/identity.sql",
    "tests/fixtures/pretty.sql",
    "tests/fixtures/partial.sql",
})
_POOL_SAFE_PREFIXES = ("sqlglot/generators/", "sqlglot/parsers/", "sqlglot/typing/")
_PICKLE_DUNDERS = frozenset({
    "__hash__", "__eq__", "__ne__", "__reduce__", "__reduce_ex__",
    "__setstate__", "__getstate__", "__new__", "__init__",
})
OPT_TPCDS = "tests/test_optimizer.py::TestOptimizer::test_tpcds"
OPT_TPCH = "tests/test_optimizer.py::TestOptimizer::test_tpch"


def _pool_safe_world(world_mask, world_changed, world_failed):
    if world_mask is None or world_failed:
        return False
    return all(
        (p.startswith(_POOL_SAFE_PREFIXES)
         or (p.startswith("sqlglot/dialects/") and p != "sqlglot/dialects/dialect.py"))
        and a not in _PICKLE_DUNDERS
        for p, _, a in world_changed
    )


class TestIndex:
    def __init__(self, tests, profile):
        self.profile = profile
        self.row = {t["id"]: t for t in tests}
        self.names = list(profile["ids"])
        known = set(self.names)
        self.names.extend(t["id"] for t in tests if t["id"] not in known)
        self.full = self.unobserved = 0
        self.by_file = defaultdict(int)
        self.durations = {}
        for i, name in enumerate(self.names):
            if (row := self.row.get(name)) is None:
                continue
            bit = 1 << i
            self.full |= bit
            self.by_file[row["file"]] |= bit
            if name not in known:
                self.unobserved |= bit
            self.durations[row["number"]] = int(row["seconds"] * 1000 + 0.5)
        self.unobserved |= profile["incomplete"] & self.full

    def candidates(self, item, root, part, single, ignore_startup=False):
        data = self.profile
        path = item.path
        reads = data["reads"].get(path, 0)
        declared = self.by_file.get(path, 0)
        if item.kind == "equivalent":
            return reads
        if item.kind == "docstring":
            return reads | data["names"].get("__doc__", 0) | self.unobserved
        if item.kind == "fixture":
            if reads:
                return self.setup_readers(path, reads, root, part) | self.unobserved
            return self.full
        if item.kind == "body":
            # A global initialized by an executed factory can affect tests which
            # never call that factory themselves.
            if not ignore_startup and item.lines.intersection(data["startup_body"].get(path, set())):
                return self.full
            line_map = data["lines"].get(path, {})
            mask = reads | self.unobserved
            for line in item.lines:
                mask |= line_map.get(line, 0)
            # A changed test can be renamed or skip execution before its first body line.
            if declared and not line_map:
                mask |= declared
            if single and part is not None:
                sensitivity = refine(item, root, part, data)
                if sensitivity is not None:
                    mask = (mask & sensitivity) | reads | self.unobserved
            return mask
        if item.kind == "binding":
            names = data["names"]
            name_mask = 0
            for name in item.names:
                name_mask |= names.get(name, 0)
            qualname = ".".join(item.scope)
            owners = data["classes"].get((path, qualname))
            if owners is not None:
                # Base-class descriptors and dispatch tables can be copied when a
                # module is imported. Any observed receiver can use the new value.
                direct = names.get(item.scope[-1], 0) & name_mask
                if name_mask and item.names.isdisjoint(data["startup_names"]):
                    owners &= name_mask
                return reads | declared | owners | direct | self.unobserved
            if single and part is not None:
                local = scalar_mask(item, part, root, data)
                if local is not None:
                    return local | self.unobserved
            # A module binding can feed imports, default arguments or cached aliases.
            return self.full
        return self.full

    def setup_readers(self, path, reads, root, part):
        """Drop readers that only meet this fixture through a class set-up they do not use."""
        try:
            classes = json.loads((APP / "setup_reads.json").read_text())
        except (OSError, ValueError):
            return reads
        for entry in classes:
            attrs = entry["files"].get(path)
            loaders = {attr for attr, target in entry["loaders"].items() if target == path}
            if not attrs or not loaders or set(attrs) != loaders or part is None:
                continue
            pairs = fixture_pairs(path, root, part)
            if pairs is None:
                continue  # the class set-up itself may fail: every test of the class stays
            old_pairs, new_pairs = pairs
            same_shape = (
                len(old_pairs) == len(new_pairs)
                and not any(len(a) != len(b) for a, b in zip(old_pairs, new_pairs))
            )
            changed = (
                {position for a, b in zip(old_pairs, new_pairs)
                 for position, (x, y) in enumerate(zip(a, b)) if x != y or type(x) is not type(y)}
                if same_shape else None
            )
            for position, name in enumerate(self.names):
                bit = 1 << position
                if not reads & bit or name not in entry["touch"]:
                    continue
                if path in entry["body"].get(name, ()):
                    continue
                used = loaders.intersection(entry["touch"][name])
                if not used:
                    reads &= ~bit
                elif same_shape and all(changed <= set(entry["unused"].get(name, {}).get(attr, ())) for attr in used):
                    reads &= ~bit
        return reads

    def selected_ids(self, mask):
        mask &= self.full
        ids = []
        while mask:
            bit = mask & -mask
            ids.append(self.names[bit.bit_length() - 1])
            mask ^= bit
        return ids


def fixture_pairs(path, root, part):
    """The pinned fixture loader run on the old and the new text of a file, or None if it fails."""
    try:
        scope = {"__file__": str(root / "tests" / "helpers.py"), "__name__": "pinned_helpers"}
        exec(compile((root / "tests" / "helpers.py").read_text(encoding="utf-8"), scope["__file__"], "exec"), scope)
        old_text = (root / path).read_text(encoding="utf-8")
        new_text, _ = apply_file(old_text, part)
        import tempfile
        relative = Path(path).relative_to("tests/fixtures")
        pairs = []
        for text in (old_text, new_text):
            with tempfile.TemporaryDirectory(prefix="fixture-") as directory:
                target = Path(directory, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                scope["FIXTURES_DIR"] = directory
                pairs.append(list(scope["load_sql_fixture_pairs"](str(relative))))
        return pairs
    except Exception:
        return None


_SLICES = None
_OPT_SLICES = None
_PHASE1 = None


def _load_slices():
    global _SLICES
    if _SLICES is None:
        try:
            _SLICES = json.loads((APP / "slices.json").read_text())
        except (OSError, ValueError):
            _SLICES = {}
    return _SLICES


def _load_opt_slices():
    global _OPT_SLICES
    if _OPT_SLICES is None:
        pkl = APP / "opt_slices.pkl.gz"
        if pkl.is_file():
            try:
                with gzip.open(pkl, "rb") as fh:
                    _OPT_SLICES = pickle.load(fh)
                return _OPT_SLICES
            except (OSError, ValueError, pickle.PickleError):
                pass
        try:
            _OPT_SLICES = json.loads((APP / "opt_slices.json").read_text())
        except (OSError, ValueError):
            _OPT_SLICES = {}
    return _OPT_SLICES


def _load_phase1():
    global _PHASE1
    if _PHASE1 is None:
        try:
            _PHASE1 = json.loads((APP / "exec_phase1.json").read_text())
        except (OSError, ValueError):
            _PHASE1 = {}
    return _PHASE1


def _unrelated_test_item(test, item, bit, index):
    if not (item.path.startswith("tests/") and item.path.endswith(".py")):
        return False
    if item.path == test.split("::")[0] or item.path in _SHARED_TEST_FILES:
        return False
    if index.profile["reads"].get(item.path, 0) & bit:
        return False
    if any(m & bit for m in index.profile["lines"].get(item.path, {}).values()):
        return False
    return True


def slice_plan(test, entry, impacts, world_paths, parts, index, root, world_fn_spans=None):
    """Elements of a fixture-driven test that the patch can reach, or None when that is not known."""
    bit = 1 << index.names.index(test)
    need = 0
    world_checked = False
    for item in impacts:
        if item.kind in ("equivalent", "docstring"):
            continue
        if _unrelated_test_item(test, item, bit, index):
            continue
        if item.kind == "fixture":
            if item.path == entry["file"]:
                pairs = fixture_pairs(item.path, root, parts.get(item.path))
                if pairs is None or len(pairs[0]) != len(pairs[1]) or len(pairs[0]) != entry["size"]:
                    return None
                for position, (old, new) in enumerate(zip(*pairs)):
                    if old != new:
                        need |= 1 << position
                continue
            raw_reads = index.profile["reads"].get(item.path, 0)
            if not (raw_reads & bit):
                continue  # this test never reads that fixture file
            if not (index.setup_readers(item.path, raw_reads, root, parts.get(item.path)) & bit):
                continue  # only touched via an unused class set-up attribute
            return None
        if item.path in world_paths:
            if not world_fn_spans:
                return None
            if not world_checked:
                world_checked = True
                for wp, (lo, hi) in world_fn_spans:
                    cover = entry["lines"].get(wp, {})
                    setup = entry["setup"].get(wp, ())
                    outer = entry.get("outer", {}).get(wp, ())
                    credited = index.profile["lines"].get(wp, {})
                    for line in range(lo, hi + 1):
                        if line in setup:
                            return None
                        hit = cover.get(line, cover.get(str(line), 0))
                        if not hit and line not in outer and credited.get(line, 0) & bit:
                            return None
                        need |= hit
            if item.kind != "body":
                continue
        if item.kind != "body" or not item.path.startswith("sqlglot/"):
            return None
        cover = entry["lines"].get(item.path, {})
        setup = entry["setup"].get(item.path, ())
        outer = entry.get("outer", {}).get(item.path, ())
        credited = index.profile["lines"].get(item.path, {})
        for line in item.lines:
            if line in setup:
                return None  # runs while the class is set up, before any element
            hit = cover.get(line, cover.get(str(line), 0))
            if not hit and line not in outer and credited.get(line, 0) & bit:
                return None  # the whole test ran this line but no single element did
            need |= hit
    return [i for i in range(entry["size"]) if need >> i & 1]


def _setup_safe(impacts, world_paths, world_mask, world_changed, world_failed, test_code):
    """True when TestExecutor.setUpClass() itself cannot be affected by the patch."""
    if test_code:
        return False
    for item in impacts:
        if item.kind == "fixture" and item.path.startswith("tests/fixtures/optimizer/tpc-"):
            return False
        if item.kind == "body" and item.path == "sqlglot/executor/table.py":
            return False
        if item.kind not in ("equivalent", "docstring", "body", "fixture") and item.path not in world_paths:
            return False
    if world_paths:
        if world_mask is None or world_failed:
            return False
        if any(p == "sqlglot/executor/table.py" for p, _, _ in world_changed):
            return False
    return True


def _phase1_safe(test, kind, impacts, world_paths, world_mask, world_changed, world_failed,
                 test_code, explained, index, root, p1_data, parts=None):
    """True when Phase-1 dry-run + fingerprint check is sound for this executor TPC test."""
    if test_code or not explained or not p1_data or test not in index.names:
        return False
    bit = 1 << index.names.index(test)
    allowed_fixture = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql" if kind == "tpcds" else "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
    row_lines = p1_data.get("row_exec_lines", {})
    for item in impacts:
        if item.kind in ("equivalent", "docstring"):
            continue
        if _unrelated_test_item(test, item, bit, index):
            continue
        if item.kind == "fixture":
            if item.path == allowed_fixture:
                continue
            raw_reads = index.profile["reads"].get(item.path, 0)
            if not (raw_reads & bit):
                continue
            if parts and not (index.setup_readers(item.path, raw_reads, root, parts.get(item.path)) & bit):
                continue
            return False
        if item.path == "sqlglot/executor/table.py":
            return False
        if item.path == "sqlglot/executor/__init__.py" and kind in ("tpcds", "tpch"):
            return False
        if item.kind == "body" and item.path not in world_paths:
            if kind == "tpcds":
                rl = row_lines.get(item.path)
                if rl and item.lines.intersection(rl):
                    return False
        elif item.path not in world_paths:
            return False
    if world_paths:
        if world_mask is None:
            return False
        if world_failed:
            import world
            _, kr = world._data()
            imps = kr.get("imports", {})
            if any(imps.get(fp, -1) & bit for fp in world_failed):
                return False
        for p, q, a in world_changed:
            if p == "sqlglot/executor/table.py":
                return False
            if p == "sqlglot/executor/__init__.py" and kind in ("tpcds", "tpch"):
                return False
            if kind == "tpcds":
                if p in _ROW_EXEC_WORLD_FILES:
                    return False
                if p == "sqlglot/helper.py" and a in _ROW_EXEC_HELPER_ATTRS:
                    return False
    return True


def patch_sources(text, root):
    result = {}
    for part in parse_patch(text):
        if (not part.valid or not part.hunks
                or Path(part.path).is_absolute() or ".." in Path(part.path).parts):
            return None
        path = root / part.path
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return None
        new_text, _ = apply_file(path.read_text(encoding="utf-8"), part)
        result[part.path] = new_text
    return result or None


_OPT_ONLY_FILES = frozenset({
    "sqlglot/optimizer/eliminate_ctes.py",
    "sqlglot/optimizer/eliminate_subqueries.py",
    "sqlglot/optimizer/isolate_table_selects.py",
    "sqlglot/optimizer/merge_subqueries.py",
    "sqlglot/optimizer/optimize_joins.py",
    "sqlglot/optimizer/pushdown_predicates.py",
    "sqlglot/optimizer/pushdown_projections.py",
    "sqlglot/optimizer/qualify.py",
    "sqlglot/optimizer/qualify_columns.py",
    "sqlglot/optimizer/qualify_tables.py",
    "sqlglot/optimizer/unnest_subqueries.py",
})


def select(text, root, index, probe_budget, debug=False, probes=True):
    # Added, deleted, renamed and unread files, and reshaped test modules, are
    # answered from recorded file and line dependencies; the rest goes on below.
    if not text.strip():
        return []  # an empty patch changes nothing
    import structure
    plain, extra = structure.resolve(text, index, root, probes=probes)
    if extra is structure.FULL:
        return schedule([row["number"] for row in index.row.values()], index.durations)
    unapplied_test_files = set()
    if plain != text:
        if debug:
            print("structural", len(index.selected_ids(extra)), "plain" if plain else "", file=sys.stderr)
        if not plain:
            return schedule([index.row[key]["number"] for key in index.selected_ids(extra)], index.durations)
        if structure.LAST_UNAPPLIED:
            probes = False
        unapplied_test_files = set(structure.LAST_UNAPPLIED_TEST_FILES)
        text = plain
    impacts = analyze(text, root)
    parts = {part.path: part for part in parse_patch(text)}
    startup = index.profile["startup_body"]
    # Anything that is not a plain edit inside a function body is judged by what
    # it changes in the imported state of the library.
    hard_world_paths = {item.path for item in impacts
                        if item.path.startswith("sqlglot/") and item.path.endswith(".py")
                        and item.kind in ("binding", "unknown")}
    startup_body_paths = {item.path for item in impacts
                          if item.path.startswith("sqlglot/") and item.path.endswith(".py")
                          and item.path not in hard_world_paths
                          and item.kind == "body" and item.lines.intersection(startup.get(item.path, ()))}
    world_paths = hard_world_paths | startup_body_paths
    body_cells = set()
    for item in impacts:
        if item.path in startup_body_paths and item.kind == "body" and item.scope and "<lambda>" not in item.scope:
            qual = ".".join(item.scope[:-1])
            body_cells.add((item.path, qual, item.scope[-1]))

    changes = None
    world_mask = None
    world_changed = []
    world_failed = []
    world_fn_spans = None
    pure_body_world = False
    if world_paths and probes:
        import world
        changes = patch_sources(text, root)
        if changes is not None:
            world_mask = world.candidates(
                root, index,
                {p: s for p, s in changes.items() if p.startswith("sqlglot/") and p.endswith(".py")},
                body_cells=body_cells or None,
            )
            if world_mask is not None:
                world_mask &= index.full
                world_changed = list(world.LAST_CHANGED)
                world_failed = list(world.LAST_FAILED)
                world_fn_spans = world.LAST_FN_SPANS
                pure_body_world = bool(world.LAST_PURE_BODY and not hard_world_paths)

    effective_world_paths = set() if pure_body_world else world_paths
    non_equiv = [item for item in impacts if item.kind not in ("equivalent", "docstring")]
    single_refine = len(non_equiv) == 1 and non_equiv[0].kind == "body"

    mask = 0
    for item in impacts:
        if world_mask is not None and item.path in hard_world_paths:
            continue
        if world_mask is not None and item.path in startup_body_paths:
            qual = ".".join(item.scope[:-1]) if item.scope else ""
            if item.kind == "body" and item.scope and (item.path, qual, item.scope[-1]) in body_cells:
                mask |= index.candidates(item, root, parts.get(item.path), single_refine, ignore_startup=True)
            continue
        mask |= index.candidates(item, root, parts.get(item.path), single_refine)
    if world_mask is not None:
        mask |= world_mask
    if world_mask is None and any(item.kind == "binding" for item in impacts) and mask != index.full:
        from probe import check_imports
        changes = patch_sources(text, root)
        if changes is None or probe_budget <= 0.25 or check_imports(root, changes, min(2.0, probe_budget)) is not True:
            mask = index.full
    ids = index.selected_ids(mask)
    weights = index.durations
    probed = False
    nominal_ms = sum(weights[index.row[key]["number"]] for key in ids)
    if debug:
        print([(item.kind, item.path) for item in impacts], len(ids), nominal_ms,
              ("world-pure" if pure_body_world else "world") if world_mask is not None else "", file=sys.stderr)

    python_edit = any(item.kind in ("body", "binding", "docstring") or item.path in world_paths for item in impacts)
    roundtrip_fixture = (
        not python_edit
        and any(item.kind == "fixture" and item.path in {
            "tests/fixtures/identity.sql",
            "tests/fixtures/pretty.sql",
            "tests/fixtures/partial.sql",
        } for item in impacts)
    )
    probe_eligible = python_edit or roundtrip_fixture
    explained = all(item.kind in ("equivalent", "docstring", "body", "binding", "fixture")
                    or (world_mask is not None and item.path in world_paths) for item in impacts)
    pool_safe = _pool_safe_world(world_mask, world_changed, world_failed)
    fragile = any(breaks_pool(item, effective_world_paths, pool_safe=pool_safe) for item in impacts)
    edited_test_files = {item.path for item in impacts
                         if item.path.startswith("tests/") and item.path.endswith(".py")
                         and item.kind not in ("equivalent", "docstring")}
    unprobeable_test_files = edited_test_files | unapplied_test_files
    shared_test_edit = bool(unprobeable_test_files & _SHARED_TEST_FILES)
    exec_test_code = shared_test_edit or ("tests/test_executor.py" in unprobeable_test_files)
    opt_test_code = shared_test_edit or ("tests/test_optimizer.py" in unprobeable_test_files)
    probe_test_block = shared_test_edit or (fragile and opt_test_code)
    setup_safe = _setup_safe(impacts, effective_world_paths, world_mask, world_changed, world_failed, exec_test_code)
    fixture_edited_tpcds = any(item.kind == "fixture" and item.path.startswith("tests/fixtures/optimizer/tpc-ds/") for item in impacts)
    fixture_edited_tpch = any(item.kind == "fixture" and item.path.startswith("tests/fixtures/optimizer/tpc-h/") for item in impacts)
    fixture_edited_tpc = fixture_edited_tpcds or fixture_edited_tpch

    number = lambda keys: [index.row[key]["number"] for key in keys]

    # Step A: Decide the expensive executor TPC tests via Phase-1 dry-run and/or lazy slices.
    slices_data = _load_slices()
    p1_data = _load_phase1()
    decided_tpc = set()

    if probes and not exec_test_code and probe_budget > 5 and any(t in ids for t in TPC_SET):
        p1_jobs = []
        p1_meta = {}
        opt_only_edit = (
            bool(non_equiv)
            and all(item.path in _OPT_ONLY_FILES and item.kind == "body" for item in non_equiv)
            and not effective_world_paths
        )
        duckdb_only_edit = (
            bool(non_equiv)
            and all(item.path == "sqlglot/generators/duckdb.py" and item.kind == "body" for item in non_equiv)
            and not effective_world_paths
        )
        for test in (TPC_CDS, TPC_H, TPC_OPT_H):
            if test not in ids or test not in index.row:
                continue
            kind = TPC_KIND[test]
            if not _phase1_safe(test, kind, impacts, effective_world_paths, world_mask,
                                world_changed, world_failed, exec_test_code, explained, index, root, p1_data, parts=parts):
                continue
            entry = slices_data.get(test)
            exp_list = p1_data.get(kind, [])
            if not exp_list:
                continue
            indices = slice_plan(test, entry, impacts, effective_world_paths, parts, index, root, world_fn_spans) if entry else None
            if indices is None:
                indices = list(range(len(exp_list)))
            if not indices:
                continue
            # Evaluate each subsequent TPC test assuming already-queued Phase-1 tests pass.
            base_ids = [k for k in ids if k not in p1_meta] if p1_meta else ids
            saving = (plan_cost(schedule(number(base_ids), weights), weights)
                      - plan_cost(schedule(number([k for k in base_ids if k != test]), weights), weights))
            if kind == "tpcds":
                n_exec = sum(1 for i in indices if exp_list[i]["exec"])
                if duckdb_only_edit:
                    p1_cpu = 80 + 4 * n_exec
                elif opt_only_edit:
                    p1_cpu = 200 + 38 * n_exec
                else:
                    p1_cpu = 250 + 65 * n_exec
            elif kind == "tpch":
                p1_cpu = 120
            else:
                p1_cpu = 140
            if debug:
                print(f"   phase1 {test.split('::')[-1]} n={len(indices)} cpu={p1_cpu} saving={saving} opt_only={opt_only_edit if kind == 'tpcds' else False}", file=sys.stderr)
            if 0.55 * saving - p1_cpu > 0:
                if kind == "tpcds" and entry:
                    # Check highest-cost queries first so broken patches trip diff_budget fast.
                    indices = sorted(indices, key=lambda i: (-int(exp_list[i]["exec"]), -entry["cost"][i]))
                p1_jobs.append({
                    "test": test,
                    "kind": kind,
                    "size": len(exp_list),
                    "indices": indices,
                    "expected": {str(i): exp_list[i] for i in indices},
                    "diff_budget": int(0.55 * saving / 0.65) if (kind == "tpcds" and entry) else None,
                    "costs": entry["cost"] if (kind == "tpcds" and entry) else None,
                    "opt_only": opt_only_edit if kind == "tpcds" else False,
                    "duckdb_only": duckdb_only_edit if kind == "tpcds" else False,
                })
                p1_meta[test] = (kind, entry, exp_list, p1_cpu)
        if p1_jobs:
            from probe import probe_phase1, probe_slices
            changes = changes or patch_sources(text, root)
            if changes:
                tot_p1_cpu = sum(m[3] for m in p1_meta.values())
                res = probe_phase1(root, changes, p1_jobs, min(probe_budget, 8.0 + 4.0 * tot_p1_cpu / 1000))
                for test, rec in res.items():
                    verdict = rec.get("phase1")
                    if debug:
                        print(f"   phase1 result {test.split('::')[-1]} {verdict} py={len(rec.get('py_diff', []))} dk={len(rec.get('dk_only', []))}", file=sys.stderr)
                    if verdict == "pass":
                        decided_tpc.add(test)
                        ids = [k for k in ids if k != test]
                        nominal_ms = sum(weights[index.row[k]["number"]] for k in ids)
                    elif verdict == "fail":
                        decided_tpc.add(test)
                    elif verdict == "diff":
                        decided_tpc.add(test)
                        kind, entry, exp_list, _ = p1_meta[test]
                        diff_idx = sorted(set(rec.get("py_diff", [])) | set(rec.get("dk_only", [])))
                        if entry and diff_idx:
                            saving = (plan_cost(schedule(number(ids), weights), weights)
                                      - plan_cost(schedule(number([k for k in ids if k != test]), weights), weights))
                            diff_idx.sort(key=lambda i: entry["cost"][i])
                            needed_tables = None
                            tpch_lazy = kind in ("tpch", "opt_tpch") and setup_safe and not fixture_edited_tpcds
                            tpcds_lazy = kind == "tpcds" and setup_safe and not fixture_edited_tpc
                            if tpcds_lazy:
                                needed_tables = sorted({t for i in diff_idx for t in exp_list[i].get("tables", ())})
                                est_setup = 100 + 50 * len(needed_tables)
                            elif tpch_lazy:
                                est_setup = 450
                            else:
                                est_setup = entry["setup_ms"]
                            lazy_cpu = est_setup + sum(entry["cost"][i] for i in diff_idx)
                            if debug:
                                print(f"   phase1->slices {test.split('::')[-1]} n={len(diff_idx)} cpu={lazy_cpu} saving={saving}", file=sys.stderr)
                            if 0.55 * saving - 0.58 * lazy_cpu > 0:
                                spec = {
                                    "test": test, "attr": entry["attr"], "indices": diff_idx, "size": entry["size"],
                                    "setup_safe": tpcds_lazy or tpch_lazy,
                                    "tables": needed_tables,
                                    "tpch_only": tpch_lazy,
                                }
                                sv = probe_slices(root, changes, spec, min(probe_budget, 5.0 + 3.0 * lazy_cpu / 1000))
                                if debug:
                                    print(f"   phase1->slices verdict {sv}", file=sys.stderr)
                                if sv is True:
                                    ids = [k for k in ids if k != test]
                                    nominal_ms = sum(weights[index.row[k]["number"]] for k in ids)

    # Fallback slice probes for TPC tests not handled by Phase-1 (e.g. edits inside executor/row-exec lines)
    for test, entry in slices_data.items():
        if not probes or test in decided_tpc or test not in ids or test not in index.row or exec_test_code or probe_budget <= 5:
            continue
        indices = slice_plan(test, entry, impacts, effective_world_paths, parts, index, root, world_fn_spans)
        if not indices:
            continue
        saving = (plan_cost(schedule(number(ids), weights), weights)
                  - plan_cost(schedule(number([key for key in ids if key != test]), weights), weights))
        indices.sort(key=lambda i: entry["cost"][i])
        kind = TPC_KIND.get(test, "")
        exp_list = p1_data.get(kind, [])
        needed_tables = None
        tpch_lazy = kind in ("tpch", "opt_tpch") and setup_safe and not fixture_edited_tpcds
        tpcds_lazy = kind == "tpcds" and setup_safe and not fixture_edited_tpc and bool(exp_list)
        if tpcds_lazy:
            needed_tables = sorted({t for i in indices for t in exp_list[i].get("tables", ())})
            est_setup = 100 + 50 * len(needed_tables)
        elif tpch_lazy:
            est_setup = 450
        else:
            est_setup = entry["setup_ms"]
        cpu = est_setup + sum(entry["cost"][i] for i in indices)
        if debug:
            print(f"   slices {test.split('::')[-1]} n={len(indices)} cpu={cpu} saving={saving}", file=sys.stderr)
        if 0.55 * saving - 0.58 * cpu <= 0:
            continue
        from probe import probe_slices
        changes = changes or patch_sources(text, root)
        if not changes:
            continue
        spec = {
            "test": test, "attr": entry["attr"], "indices": indices, "size": entry["size"],
            "setup_safe": tpcds_lazy or tpch_lazy,
            "tables": needed_tables,
            "tpch_only": tpch_lazy,
        }
        verdict = probe_slices(root, changes, spec, min(probe_budget, 5.0 + 3.0 * cpu / 1000))
        if debug:
            print(f"   slices verdict {verdict}", file=sys.stderr)
        if verdict is True:
            ids = [key for key in ids if key != test]
            nominal_ms = sum(weights[index.row[key]["number"]] for key in ids)

    # Prepare transparent fixture slices for the 8 heavy fixture-driven TestOptimizer tests.
    opt_slices_data = _load_opt_slices()
    fixture_slices = {}
    extra_override = {}
    if probes and ids and not fragile and not opt_test_code and explained:
        if _PROBE_COST is None:
            probe_cpu_ms([], 0)
        tmap = (_PROBE_COST or {}).get("tests", {})
        fmap = (_PROBE_COST or {}).get("files", {})
        for test in ids:
            entry = opt_slices_data.get(test)
            if not entry or test not in index.row:
                continue
            idxs = slice_plan(test, entry, impacts, effective_world_paths, parts, index, root, world_fn_spans)
            if idxs is not None and len(idxs) < entry["size"]:
                idxs.sort(key=lambda i: entry["cost"][i])
                fixture_slices[test] = {"rel": entry["rel"], "indices": idxs, "size": entry["size"]}
                full_extra = max(0, tmap.get(test, 0) - fmap.get(test.split("::")[0], 0))
                tot_c = sum(entry["cost"])
                part_c = sum(entry["cost"][i] for i in idxs)
                outer_ms = entry.get("outer_ms", 0)
                extra_override[test] = min(full_extra, outer_ms + int(full_extra * (part_c / max(1, tot_c))))

    from probe import _SQLGLOT_IMPORT_FILES
    use_sg = (
        bool(non_equiv)
        and not unprobeable_test_files
        and all(
            item.path.startswith(("sqlglot/", "tests/fixtures/"))
            and item.path not in _SQLGLOT_IMPORT_FILES
            for item in non_equiv
        )
    )

    # Check whether Step B will already run directly (so Step A2 doesn't duplicate a sliced test).
    stepB_ready = False
    if probes and ids and len(ids) <= 130 and probe_budget > 0.5 and probe_eligible and explained and not probe_test_block:
        _chosen0 = set(ids)
        _casc0 = []
        if fragile:
            for users in pool_groups(index):
                hit = [key for key in users if key in _chosen0]
                if hit:
                    _casc0.extend(key for key in users[users.index(hit[0]) + 1:] if key not in _chosen0)
        _unp0 = plan_cost(schedule(number(ids + _casc0), weights), weights)
        _pln0 = plan_cost(schedule(number(ids), weights), weights)
        _sk0 = setup_safe and not (_chosen0 & TPC_SET)
        _cpu0 = probe_cpu_ms(ids, nominal_ms, skip_exec_setup=_sk0, extra_override=extra_override, use_sg=use_sg)
        stepB_ready = 0.45 * _unp0 + 0.55 * max(0.0, _unp0 - 0.95 * _pln0) - _cpu0 > 0

    # Step A2: Decide heavy fixture-driven TestOptimizer tests (test_tpcds, test_tpch) via fail-fast slices
    # when Step B will not already run them sliced in one fork.
    known_fail = set()
    for test in (OPT_TPCDS, OPT_TPCH):
        entry = opt_slices_data.get(test)
        if not entry or not probes or test not in ids or test not in index.row or opt_test_code or not explained or probe_budget <= 3:
            continue
        if stepB_ready and test in fixture_slices:
            continue
        indices = slice_plan(test, entry, impacts, effective_world_paths, parts, index, root, world_fn_spans)
        if indices is not None and not indices:
            continue
        if indices is None:
            if len(ids) > 25:
                continue
            indices = list(range(entry["size"]))
        rem_ids = [k for k in ids if k != test]
        base_ms = plan_cost(schedule(number(ids), weights), weights)
        rem_ms = plan_cost(schedule(number(rem_ids), weights), weights)
        saving = base_ms - rem_ms
        # Bonus if removing this heavy test makes the remaining candidate list worth probing.
        unlock_bonus = 0.0
        if rem_ids and len(rem_ids) <= 45 and python_edit:
            rem_nom = sum(weights[index.row[k]["number"]] for k in rem_ids)
            rem_skip = setup_safe and not (set(rem_ids) & TPC_SET)
            rem_cpu = probe_cpu_ms(rem_ids, rem_nom, skip_exec_setup=rem_skip, extra_override=extra_override, use_sg=use_sg)
            if 0.48 * rem_ms - rem_cpu > 0:
                unlock_bonus = 0.40 * (rem_ms - rem_cpu)
        est_cpu = 80 + int(0.38 * sum(entry["cost"][i] for i in indices))
        eff_saving = saving + unlock_bonus
        if debug:
            print(f"   opt-slices {test.split('::')[-1]} n={len(indices)} cpu={est_cpu} saving={saving} eff={eff_saving:.0f}", file=sys.stderr)
        if est_cpu > 4500 or 0.55 * eff_saving - est_cpu <= 0:
            continue
        from probe import probe_opt_slices
        changes = changes or patch_sources(text, root)
        if not changes:
            continue
        indices.sort(key=lambda i: entry["cost"][i])
        spec = {"test": test, "rel": entry["rel"], "indices": indices, "size": entry["size"]}
        verdict, broke = probe_opt_slices(root, changes, spec, min(probe_budget, 4.0 + 3.0 * est_cpu / 1000))
        if debug:
            print(f"   opt-slices verdict {verdict} broke={broke}", file=sys.stderr)
        if verdict is True and not broke:
            ids = rem_ids
            nominal_ms = sum(weights[index.row[k]["number"]] for k in ids)
        elif verdict is False and not fragile:
            known_fail.add(test)

    chosen = set(ids)
    pool_set = {k for users in pool_groups(index) for k in users}
    cascade = []
    if fragile:
        for users in pool_groups(index):
            hit = [key for key in users if key in chosen]
            if hit:
                cascade.extend(key for key in users[users.index(hit[0]) + 1:] if key not in chosen)
    # Unsliced non-roundtrip, non-TPC fixture edits almost always break their fixture reader;
    # keep those readers conservatively without burning unsliced probe CPU on them.
    fixed_fx = set()
    if python_edit:
        for item in impacts:
            if (item.kind == "fixture" and item.path not in _RT_FIXTURES
                    and not item.path.startswith("tests/fixtures/optimizer/tpc-")):
                fixed_fx.update(
                    k for k in index.selected_ids(index.profile["reads"].get(item.path, 0))
                    if k in chosen and k not in fixture_slices
                )
        if fragile and (fixed_fx & pool_set):
            fixed_fx.clear()
    fixed_tests = {k for k in ids if k.split("::")[0] in unprobeable_test_files}
    fixed_keep = fixed_fx | fixed_tests
    probe_ids = [k for k in ids if k not in fixed_keep] if fixed_keep else ids
    probe_nom = sum(weights[index.row[k]["number"]] for k in probe_ids)
    unprobed_ms = plan_cost(schedule(number(ids + cascade), weights), weights)
    plain_ms = plan_cost(schedule(number(ids), weights), weights)
    fixed_ms = plan_cost(schedule(number(list(fixed_keep)), weights), weights) if fixed_keep else 0
    skip_exec_setup = setup_safe and not (set(probe_ids) & TPC_SET)
    cpu_ms = probe_cpu_ms(probe_ids, probe_nom, skip_exec_setup=skip_exec_setup, extra_override=extra_override, use_sg=use_sg)
    pot_saving = unprobed_ms - fixed_ms
    worth = bool(probe_ids) and (0.45 * pot_saving + 0.55 * max(0.0, unprobed_ms - 0.95 * plain_ms) - cpu_ms > 0)
    if debug and ids:
        print("   probe-data " + json.dumps({"ids": number(probe_ids), "cascade": number(cascade), "cpu": round(cpu_ms),
                                             "ok": bool(probe_eligible and explained and not probe_test_block and len(probe_ids) <= 130),
                                             "sliced": len(fixture_slices), "fixed": len(fixed_keep), "sg": use_sg}),
              file=sys.stderr)
        print(f"   probe estimate cpu={cpu_ms:.0f} unprobed={unprobed_ms} worth={worth} skip_setup={skip_exec_setup} fragile={fragile}", file=sys.stderr)
    def _test_caps(keys):
        if fragile:
            return None
        if _PROBE_COST is None:
            probe_cpu_ms([], 0)
        tm = (_PROBE_COST or {}).get("tests", {})
        fm = (_PROBE_COST or {}).get("files", {})
        caps = {}
        for k in keys:
            full_ex_ms = max(0, tm.get(k, weights[index.row[k]["number"]]) - fm.get(k.split("::")[0], 0))
            caps[k] = max(2.5, 5.0 * full_ex_ms / 1000.0)
        return caps

    full_probed = False
    broken_users = set()
    if (probes and probe_ids and worth and len(probe_ids) <= 130 and probe_budget > 0.5 and probe_eligible and explained
            and not probe_test_block):
        from probe import BROKEN, COMPLETE, probe
        changes = changes or patch_sources(text, root)
        if changes:
            probe_ids = sorted(probe_ids, key=lambda key: index.row[key]["number"])
            deadline = min(probe_budget, max(3.0, 2.5 + 3.5 * cpu_ms / 1000))
            sub_slices = {k: fixture_slices[k] for k in probe_ids if k in fixture_slices} if not fragile else None
            passing = probe(root, changes, probe_ids, deadline, skip_executor_setup=skip_exec_setup,
                            failfast_subtest=(not fragile), fixture_slices=sub_slices,
                            per_test_caps=_test_caps(probe_ids))
            probed = bool(COMPLETE)
            broken_users.update(BROKEN)
            full_probed = True
            ids = [key for key in ids if key not in passing]

    # Step C: Selective probing when full probe did not run.
    if not full_probed and probes and ids and probe_budget > 2.0 and python_edit and explained and not probe_test_block:
        # C1: Settle optimizer pool cascade by probing only the hit pool users.
        if fragile and cascade:
            hit_users = []
            for users in pool_groups(index):
                hit_users.extend(key for key in users if key in chosen)
            casc_saving = unprobed_ms - plain_ms
            if hit_users and len(hit_users) <= 30 and casc_saving >= 1500:
                hit_nom = sum(weights[index.row[k]["number"]] for k in hit_users)
                hit_cpu = probe_cpu_ms(hit_users, hit_nom, skip_exec_setup=True, use_sg=use_sg)
                hit_set = set(hit_users)
                without_hit_ms = plan_cost(schedule(number([k for k in ids if k not in hit_set]), weights), weights)
                exp_saving = 0.85 * casc_saving + 0.45 * (plain_ms - without_hit_ms)
                if debug:
                    print(f"   cascade-probe n={len(hit_users)} cpu={hit_cpu:.0f} casc_saving={casc_saving} exp={exp_saving:.0f}", file=sys.stderr)
                if exp_saving > hit_cpu:
                    from probe import BROKEN, COMPLETE, probe
                    changes = changes or patch_sources(text, root)
                    if changes:
                        hit_users.sort(key=lambda key: index.row[key]["number"])
                        passing = probe(root, changes, hit_users, min(probe_budget, max(3.0, 2.5 + 3.5 * hit_cpu / 1000)), skip_executor_setup=True)
                        probed = bool(COMPLETE)
                        broken_users.update(BROKEN)
                        ids = [key for key in ids if key not in passing]
                        chosen = set(ids)
        # C2: Probe standalone high-ROI bottleneck tests (plus tied-pair fallback).
        bottlenecks = []
        base_cost = plan_cost(schedule(number(ids), weights), weights)
        c2_eligible = []
        for key in sorted(ids, key=lambda k: -weights[index.row[k]["number"]]):
            if key in TPC_SET or key in known_fail or key in fixed_keep or (fragile and key in pool_set):
                continue
            w_ms = weights[index.row[key]["number"]]
            if w_ms < 1000:
                break
            c2_eligible.append(key)
            rem_cost = plan_cost(schedule(number([k for k in ids if k != key]), weights), weights)
            saving = base_cost - rem_cost
            scpu = probe_cpu_ms([key], w_ms, skip_exec_setup=setup_safe, extra_override=extra_override, use_sg=use_sg)
            if saving > 0 and 0.48 * saving > scpu:
                bottlenecks.append((key, saving, scpu))
            if len(bottlenecks) >= 3:
                break
        sub = [b[0] for b in bottlenecks]
        if not sub and len(c2_eligible) >= 2:
            pair = c2_eligible[:2]
            pair_sav = base_cost - plan_cost(schedule(number([k for k in ids if k not in set(pair)]), weights), weights)
            pair_cpu = probe_cpu_ms(pair, sum(weights[index.row[k]["number"]] for k in pair),
                                    skip_exec_setup=setup_safe, extra_override=extra_override, use_sg=use_sg)
            if pair_sav >= 1500 and 0.48 * pair_sav > pair_cpu:
                sub = pair
        if sub:
            sub_cpu = probe_cpu_ms(sub, sum(weights[index.row[k]["number"]] for k in sub),
                                   skip_exec_setup=setup_safe, extra_override=extra_override, use_sg=use_sg)
            sub_saving = base_cost - plan_cost(schedule(number([k for k in ids if k not in set(sub)]), weights), weights)
            if debug:
                print(f"   bottleneck-probe n={len(sub)} cpu={sub_cpu:.0f} saving={sub_saving}", file=sys.stderr)
            if 0.48 * sub_saving > sub_cpu:
                from probe import BROKEN, probe
                changes = changes or patch_sources(text, root)
                if changes:
                    sub.sort(key=lambda key: index.row[key]["number"])
                    sub_slices = {k: fixture_slices[k] for k in sub if k in fixture_slices} if not fragile else None
                    passing = probe(root, changes, sub, min(probe_budget, max(3.0, 2.5 + 3.5 * sub_cpu / 1000)),
                                    skip_executor_setup=setup_safe, failfast_subtest=(not fragile),
                                    fixture_slices=sub_slices, per_test_caps=_test_caps(sub))
                    broken_users.update(BROKEN)
                    ids = [key for key in ids if key not in passing]
                    base_cost = plan_cost(schedule(number(ids), weights), weights)
        # C3: If a few expensive-to-probe tests blocked Step B, probe the cheap remainder if profitable.
        if ids and len(ids) <= 130:
            eligible = [k for k in ids if k not in TPC_SET and k not in known_fail and k not in fixed_keep and not (fragile and k in pool_set)]
            if len(eligible) >= 2:
                # Sort by per-test incremental probe cost descending and peel off up to 8 heavy blockers.
                tmap = (_PROBE_COST or {}).get("tests", {})
                fmap = (_PROBE_COST or {}).get("files", {})
                by_cost = sorted(
                    eligible,
                    key=lambda k: -(extra_override[k] if k in extra_override else (tmap.get(k, weights[index.row[k]["number"]]) - fmap.get(k.split("::")[0], 0))),
                )
                best_sub = None
                for drop_k in range(0 if known_fail else 1, min(9, len(by_cost))):
                    cand = by_cost[drop_k:]
                    cand_set = set(cand)
                    c_skip = setup_safe and not (cand_set & TPC_SET)
                    c_cpu = probe_cpu_ms(cand, sum(weights[index.row[k]["number"]] for k in cand),
                                         skip_exec_setup=c_skip, extra_override=extra_override, use_sg=use_sg)
                    c_sav = base_cost - plan_cost(schedule(number([k for k in ids if k not in cand_set]), weights), weights)
                    margin = 0.50 * c_sav - c_cpu
                    if margin > 300 and (best_sub is None or margin > best_sub[0]):
                        best_sub = (margin, cand, c_cpu, c_sav, c_skip)
                if best_sub is not None:
                    _, sub, sub_cpu, sub_saving, sub_skip = best_sub
                    if debug:
                        print(f"   subset-probe n={len(sub)}/{len(ids)} cpu={sub_cpu:.0f} saving={sub_saving}", file=sys.stderr)
                    from probe import BROKEN, probe
                    changes = changes or patch_sources(text, root)
                    if changes:
                        sub.sort(key=lambda key: index.row[key]["number"])
                        sub_slices = {k: fixture_slices[k] for k in sub if k in fixture_slices} if not fragile else None
                        passing = probe(root, changes, sub, min(probe_budget, max(3.0, 2.5 + 3.5 * sub_cpu / 1000)),
                                        skip_executor_setup=sub_skip, failfast_subtest=(not fragile),
                                        fixture_slices=sub_slices, per_test_caps=_test_caps(sub))
                        broken_users.update(BROKEN)
                        ids = [key for key in ids if key not in passing]

    if (fragile or broken_users) and (ids or broken_users):
        chosen = set(ids)
        for users in pool_groups(index):
            if probed or not fragile:
                breakers = [key for key in users if key in broken_users]
                first = breakers[0] if breakers else None
            else:
                hit = [key for key in users if key in chosen]
                first = hit[0] if hit else None
            if first is not None:
                ids.extend(key for key in users[users.index(first) + 1:] if key not in chosen)
    if extra:
        chosen = set(ids)
        ids.extend(key for key in index.selected_ids(extra) if key not in chosen)
    numbers = [index.row[nodeid]["number"] for nodeid in ids]
    return schedule(numbers, weights)


_POOL_LINES = None


def breaks_pool(item, world_paths, pool_safe=False):
    """Can this edit make unpickling at the process-pool boundary fail?"""
    global _POOL_LINES
    if item.kind in ("equivalent", "docstring", "fixture") or not item.path.endswith(".py"):
        return False
    if item.path in world_paths and pool_safe:
        return False
    if item.kind != "body" or item.path in world_paths:
        return True
    if _POOL_LINES is None:
        try:
            _POOL_LINES = {path: frozenset(lines) for path, lines
                           in json.loads((APP / "pool_lines.json").read_text()).items()}
        except (OSError, ValueError):
            return True
    return bool(item.lines & _POOL_LINES.get(item.path, frozenset()))


_PROBE_COST = None


def probe_cpu_ms(ids, nominal_ms, skip_exec_setup=False, extra_override=None, use_sg=False):
    """Measured cost of a forked zygote probe: base import overhead + marginal per-file + per-test."""
    global _PROBE_COST
    if _PROBE_COST is None:
        try:
            _PROBE_COST = json.loads((APP / "probe_cost.json").read_text())
        except (OSError, ValueError):
            _PROBE_COST = {}
    if not ids:
        return 0
    table = _PROBE_COST
    files = {key.split("::")[0] for key in ids}
    if table and all(key in table["tests"] for key in ids):
        # Pre-warmed fork zygote eliminates Python startup and amortizes numpy/pandas/duckdb imports.
        cost = 20
        for name in files:
            fc = table["files"][name]
            if skip_exec_setup and name == "tests/test_executor.py":
                fc = table["files"].get("tests/test_optimizer.py", 584)
            cost += fc - table["base"]
        cost -= table["library"] * (len(files) - 1)
        if use_sg:
            cost -= 110
        if files & {"tests/test_executor.py", "tests/test_optimizer.py"}:
            cost -= 340
            if "tests/test_executor.py" in files and "tests/test_optimizer.py" in files:
                cost -= 320
        cost = max(cost, 30)
        extra = sum(
            (extra_override[key] if (extra_override and key in extra_override)
             else (table["tests"][key] - table["files"][key.split("::")[0]]))
            for key in ids
        )
        return 0.52 * (cost + extra)
    cost = 250 + 35 * len(files) + 1.0 * nominal_ms
    if files & {"tests/test_executor.py", "tests/test_optimizer.py"}:
        cost += 400
    if "tests/test_executor.py" in files and not skip_exec_setup:
        cost += 1600
    return cost


def plan_cost(workers, weights):
    return max(sum(weights[n] for n in w) for w in workers) + 2000 * len(workers) if workers else 0


def pool_groups(index):
    """Tests that share one process pool, per test class, in the order pytest runs them."""
    try:
        users = json.loads((APP / "pool_users.json").read_text())
    except (OSError, ValueError):
        return []
    groups = defaultdict(list)
    for key in users:
        if key in index.row:
            groups[key.rpartition("::")[0]].append(key)
    out = []
    for name, members in groups.items():
        if name.endswith("::TestOptimizer"):
            out.append(sorted(members, key=lambda key: index.row[key]["number"]))
    return out


def _cpu():
    import resource
    own, kids = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    live = 0.0
    mod = sys.modules.get("probe")
    if mod is not None:
        live = sum(getattr(mod, "_LIVE_ZYGOTE_CPU", {}).values())
    return own.ru_utime + own.ru_stime + kids.ru_utime + kids.ru_stime + live


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--map", type=Path, default=APP / "map.pkl.gz")
    parser.add_argument("--no-probe", action="store_true", help="Use measured dependencies only")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    start = time.monotonic()
    tests = json.loads((args.data / "tests.json").read_bytes())["tests"]
    with gzip.open(args.map, "rb") as stream:
        index = TestIndex(tests, pickle.load(stream))
    full = schedule([t["number"] for t in tests], index.durations)
    root = args.source.resolve()
    patches = sorted((args.data / "commits").glob("*.patch"))
    # Practice has 40 commits (180 s wall limit); final has ~60 commits (270 s wall limit).
    total_budget = 155.0 if len(patches) <= 45 else 235.0
    for path in patches:
        try:
            if args.debug:
                print(path.stem, end=" ", file=sys.stderr)
            budget = max(0.0, total_budget - (time.monotonic() - start))
            before = _cpu()
            workers = select(path.read_text(encoding="utf-8"), root, index, budget, args.debug, not args.no_probe)
            if args.debug:
                print(f"   cpu {_cpu() - before:.2f}s", file=sys.stderr)
        except (OSError, ValueError, RecursionError, SyntaxError, UnicodeError) as error:
            print(f"Conservative schedule for {path.stem}: {type(error).__name__}", file=sys.stderr)
            workers = full
        print(json.dumps({"commit": path.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
