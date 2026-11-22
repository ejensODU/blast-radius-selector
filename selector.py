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

    def candidates(self, item, root, part, single):
        data = self.profile
        path = item.path
        reads = data["reads"].get(path, 0)
        declared = self.by_file.get(path, 0)
        if item.kind == "equivalent":
            return reads
        if item.kind == "fixture":
            if reads:
                return self.setup_readers(path, reads, root, part) | self.unobserved
            return self.full
        if item.kind == "body":
            # A global initialized by an executed factory can affect tests which
            # never call that factory themselves.
            if item.lines.intersection(data["startup_body"].get(path, set())):
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
        """Drop readers that only meet this fixture through a class set-up they do not use.

        setUpClass loads some fixtures into class attributes for every test of the
        class. A test is reached by an edit only if it reads such an attribute and
        inspects a position of the loaded pairs that the edit changed (recorded per
        test in setup_reads.json), or opens the file itself.
        """
        try:
            classes = json.loads((APP / "setup_reads.json").read_text())
        except (OSError, ValueError):
            return reads
        for entry in classes:
            attrs = entry["files"].get(path)
            loaders = {attr for attr, target in entry["loaders"].items() if target == path}
            if not attrs or not loaders or set(attrs) != loaders or part is None:
                continue
            try:
                # The pinned loader, run on both versions of the file.
                scope = {"__file__": str(root / "tests" / "helpers.py"), "__name__": "pinned_helpers"}
                exec(compile((root / "tests" / "helpers.py").read_text(encoding="utf-8"), scope["__file__"], "exec"), scope)
                old_text = (root / path).read_text(encoding="utf-8")
                new_text, _ = apply_file(old_text, part)
                import tempfile
                pairs = []
                for text in (old_text, new_text):
                    with tempfile.TemporaryDirectory(prefix="fixture-") as directory:
                        target = Path(directory, Path(path).relative_to("tests/fixtures"))
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(text, encoding="utf-8")
                        scope["FIXTURES_DIR"] = directory
                        pairs.append(list(scope["load_sql_fixture_pairs"](str(Path(path).relative_to("tests/fixtures")))))
            except Exception:
                continue  # the class set-up itself may fail: every test of the class stays
            old_pairs, new_pairs = pairs
            if len(old_pairs) != len(new_pairs):
                continue
            changed = {position for a, b in zip(old_pairs, new_pairs)
                       for position, (x, y) in enumerate(zip(a, b)) if x != y or type(x) is not type(y)}
            if any(len(a) != len(b) for a, b in zip(old_pairs, new_pairs)):
                continue
            for position, name in enumerate(self.names):
                bit = 1 << position
                if not reads & bit or name not in entry["touch"]:
                    continue
                if path in entry["body"].get(name, ()):
                    continue
                used = loaders.intersection(entry["touch"][name])
                if all(changed <= set(entry["unused"].get(name, {}).get(attr, ())) for attr in used):
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


def select(text, root, index, probe_budget, debug=False, probes=True):
    # Added, deleted, renamed and unread files, and reshaped test modules, are
    # answered from recorded file and line dependencies; the rest goes on below.
    if not text.strip():
        return []  # an empty patch changes nothing
    import structure
    plain, extra = structure.resolve(text, index, root)
    if extra is structure.FULL:
        return schedule([row["number"] for row in index.row.values()], index.durations)
    if plain != text:
        if debug:
            print("structural", len(index.selected_ids(extra)), "plain" if plain else "", file=sys.stderr)
        if not plain:
            return schedule([index.row[key]["number"] for key in index.selected_ids(extra)], index.durations)
        text = plain
    impacts = analyze(text, root)
    parts = {part.path: part for part in parse_patch(text)}
    startup = index.profile["startup_body"]
    # Anything that is not a plain edit inside a function body is judged by what
    # it changes in the imported state of the library.
    world_paths = {item.path for item in impacts
                   if item.path.startswith("sqlglot/") and item.path.endswith(".py")
                   and (item.kind in ("binding", "unknown")
                        or (item.kind == "body" and item.lines.intersection(startup.get(item.path, ()))))}
    changes = None
    world_mask = None
    if world_paths and probes:
        import world
        changes = patch_sources(text, root)
        if changes is not None:
            world_mask = world.candidates(root, index, {p: s for p, s in changes.items() if p.startswith("sqlglot/") and p.endswith(".py")})
            if world_mask is not None:
                world_mask &= index.full
    mask = 0
    for item in impacts:
        if world_mask is not None and item.path in world_paths:
            continue
        mask |= index.candidates(item, root, parts.get(item.path), len(parts) == 1 and len(impacts) == 1)
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
              "world" if world_mask is not None else "", file=sys.stderr)

    # A test whose task kills a worker of the class-level process pool takes
    # down every later test that shares the pool, whatever those tests run. A
    # worker only dies while unpickling what crosses the pool boundary, so an
    # edit inside a function body matters here only on a line recorded there.
    python_edit = any(item.kind in ("body", "binding") or item.path in world_paths for item in impacts)
    explained = all(item.kind in ("equivalent", "body", "binding", "fixture")
                    or (world_mask is not None and item.path in world_paths) for item in impacts)
    fragile = any(breaks_pool(item, world_paths) for item in impacts)
    chosen = set(ids)
    cascade = []
    if fragile:
        for users in pool_groups(index):
            hit = [key for key in users if key in chosen]
            if hit:
                cascade.extend(key for key in users[users.index(hit[0]) + 1:] if key not in chosen)
    number = lambda keys: [index.row[key]["number"] for key in keys]
    # Without a probe the whole list runs, plus every later user of a pool in doubt.
    unprobed_ms = plan_cost(schedule(number(ids + cascade), weights), weights)
    # A probe in a fresh interpreter costs CPU seconds one for one. About half of
    # the lists it is tried on turn out to contain no failing test at all; when
    # some test does fail, roughly a worker start and the failing share remain.
    remaining_ms = 2000 + 0.4 * nominal_ms
    cpu_ms = probe_cpu_ms(ids, nominal_ms)
    worth = unprobed_ms - 0.5 * remaining_ms - cpu_ms > 0
    # A test run alone says nothing about state its own set-up or tear-down leaves
    # for others, so edited test code is not probed.
    test_code = any(item.path.startswith("tests/") and item.path.endswith(".py") and item.kind != "equivalent"
                    for item in impacts)
    if (probes and ids and worth and len(ids) <= 130 and probe_budget > 0.5 and python_edit and explained
            and not test_code):
        from probe import probe
        changes = changes or patch_sources(text, root)
        if changes:
            ids.sort(key=lambda key: index.row[key]["number"])
            # The limit only has to stop a patch that hangs; leave room for a slower machine.
            deadline = min(probe_budget, 3.0 + 3.0 * cpu_ms / 1000)
            passing = probe(root, changes, ids, deadline)
            # Only a probe that accounted for every test says anything about the pool.
            from probe import COMPLETE
            probed = bool(COMPLETE)
            ids = [key for key in ids if key not in passing]
    if fragile and ids:
        chosen = set(ids)
        for users in pool_groups(index):
            hit = [key for key in users if key in chosen]
            if not hit:
                continue
            if probed:
                from probe import BROKEN
                breakers = [key for key in users if key in BROKEN]
                first = breakers[0] if breakers else None
            else:
                first = hit[0]
            if first is not None:
                ids.extend(key for key in users[users.index(first) + 1:] if key not in chosen)
    if extra:
        chosen = set(ids)
        ids.extend(key for key in index.selected_ids(extra) if key not in chosen)
    numbers = [index.row[nodeid]["number"] for nodeid in ids]
    return schedule(numbers, weights)


_POOL_LINES = None


def breaks_pool(item, world_paths):
    """Can this edit make unpickling at the process-pool boundary fail?"""
    global _POOL_LINES
    if item.kind in ("equivalent", "fixture") or not item.path.endswith(".py"):
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


def probe_cpu_ms(ids, nominal_ms):
    """Measured cost of a fresh interpreter running these tests: start-up, imports, class set-up."""
    files = {key.split("::")[0] for key in ids}
    cost = 300 + 40 * len(files) + 1.15 * nominal_ms
    if files & {"tests/test_executor.py", "tests/test_optimizer.py"}:
        cost += 450  # pandas and duckdb
    if "tests/test_executor.py" in files:
        cost += 1750  # TestExecutor.setUpClass loads the TPC tables
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
    return own.ru_utime + own.ru_stime + kids.ru_utime + kids.ru_stime


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
    for path in sorted((args.data / "commits").glob("*.patch")):
        try:
            if args.debug:
                print(path.stem, end=" ", file=sys.stderr)
            budget = max(0.0, 150.0 - (time.monotonic() - start))
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
