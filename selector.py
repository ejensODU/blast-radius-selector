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
                return reads | self.unobserved
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
    impacts = analyze(text, root)
    parts = {part.path: part for part in parse_patch(text)}
    mask = 0
    for item in impacts:
        mask |= index.candidates(item, root, parts.get(item.path), len(parts) == 1 and len(impacts) == 1)
    changes = None
    if any(item.kind == "binding" for item in impacts) and mask != index.full:
        from probe import check_imports
        changes = patch_sources(text, root)
        if changes is None or probe_budget <= 0.25 or check_imports(root, changes, min(2.0, probe_budget)) is not True:
            mask = index.full
    ids = index.selected_ids(mask)
    weights = index.durations
    nominal_ms = sum(weights[index.row[key]["number"]] for key in ids)
    if debug:
        print([(item.kind, item.path) for item in impacts], len(ids), nominal_ms, file=sys.stderr)

    # A fresh interpreter can validate a short candidate list for a fraction of
    # the 2 seconds starting even one worker costs. Keep full coverage when a
    # check is too costly or the result is indeterminate.
    if (probes and ids and nominal_ms <= 700 and len(ids) <= 130 and probe_budget > 0.5
            and any(item.kind in ("body", "binding") for item in impacts)
            and all(item.kind in ("equivalent", "body", "binding") for item in impacts)):
        from probe import probe
        changes = changes or patch_sources(text, root)
        if changes:
            # The manifest measures steady-state execution; a fresh process can
            # also pay import and class setup costs. Limit that extra CPU spend.
            deadline = min(probe_budget, 0.85 + nominal_ms / 1000)
            passing = probe(root, changes, ids, deadline)
            ids = [key for key in ids if key not in passing]
    numbers = [index.row[nodeid]["number"] for nodeid in ids]
    return schedule(numbers, weights)


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
            budget = max(0.0, 65.0 - (time.monotonic() - start))
            workers = select(path.read_text(encoding="utf-8"), root, index, budget, args.debug, not args.no_probe)
        except (OSError, ValueError, RecursionError, SyntaxError, UnicodeError) as error:
            print(f"Conservative schedule for {path.stem}: {type(error).__name__}", file=sys.stderr)
            workers = full
        print(json.dumps({"commit": path.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
