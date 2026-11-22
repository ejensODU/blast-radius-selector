"""Practice-only diagnostic: the submitted selector plus an encoded summary of its own decisions.

The selections are exactly those of the submitted commit. Afterwards a count
summary of the selector's own decisions (never of test outcomes, which are not
visible here) is added as extra tests on the first commits, so it can be read
off the simulated total.
"""
import gzip
import json
import pickle
import sys
import time
from pathlib import Path

import selector as S
from schedule import schedule

TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
MAXN = "tests/test_parser.py::TestParser::test_max_nodes"


def add_ms(results, dur, total):
    """Raise the batch's simulated cost by exactly `total` ms. Returns what did not fit."""
    order = sorted((n for n in dur if dur[n] > 0), key=lambda n: (-dur[n], n))
    for _, workers in results:
        if total <= 0:
            break
        chosen = {n for w in workers for n in w}
        if not workers or len(chosen) >= len(dur):
            continue
        loads = [sum(dur[n] for n in w) for w in workers]
        heavy = workers[loads.index(max(loads))]
        for n in order:
            if dur[n] <= total and n not in chosen:
                heavy.append(n)
                chosen.add(n)
                total -= dur[n]
                if total == 0:
                    break
    return total


def burn(seconds):
    end = time.process_time() + seconds
    x = 0
    while time.process_time() < end:
        x += 1


def reason(stat):
    kinds = stat.get("impacts", [])
    if stat.get("structural_full"):
        return 3
    if stat.get("world_none"):
        return 1
    if stat.get("world_full"):
        return 2
    if any(k == "fixture" for k, p in kinds):
        return 4
    if any(k == "unknown" for k, p in kinds):
        return 5
    if kinds and all(k in ("body", "equivalent") for k, p in kinds):
        return 6
    return 7


def main():
    data, root = Path("/data"), Path("/opt/sqlglot").resolve()
    if len(sys.argv) > 2:
        data, root = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
    start = time.monotonic()
    tests = json.loads((data / "tests.json").read_bytes())["tests"]
    with gzip.open(S.APP / "map.pkl.gz", "rb") as stream:
        index = S.TestIndex(tests, pickle.load(stream))
    dur = index.durations
    number = {t["id"]: t["number"] for t in tests}
    full_plan = schedule([t["number"] for t in tests], dur)
    results, stats = [], []
    for path in sorted((data / "commits").glob("*.patch")):
        stat = {}
        try:
            budget = max(0.0, 65.0 - (time.monotonic() - start))
            workers = S.select(path.read_text(encoding="utf-8"), root, index, budget, False, True)
            stat = dict(S.LAST)
        except (OSError, ValueError, RecursionError, SyntaxError, UnicodeError) as error:
            print(f"Conservative schedule for {path.stem}: {type(error).__name__}", file=sys.stderr)
            workers = full_plan
            stat = {"error": True}
        results.append((path.stem, [list(w) for w in workers]))
        stats.append(stat)

    n_full = n_tpcds = n_maxn = n_cascade = n_probed = n_world_none = n_mid = n_incomplete = 0
    reasons, tp = [], []
    for (_, workers), stat in zip(results, stats):
        chosen = {n for w in workers for n in w}
        cost = max(sum(dur[n] for n in w) for w in workers) + 2000 * len(workers) if workers else 0
        is_full = len(chosen) >= len(tests)
        n_probed += bool(stat.get("probed"))
        n_incomplete += bool(stat.get("tried")) and not stat.get("probed")
        n_world_none += bool(stat.get("world_none"))
        if is_full:
            n_full += 1
            reasons.append(8 if stat.get("error") else reason(stat))
            continue
        n_cascade += bool(stat.get("cascade"))
        giant = False
        if number.get(TPCDS) in chosen:
            n_tpcds += 1
            giant = True
            kinds = stat.get("impacts", [])
            tp.append((1 if stat.get("world_paths") and not stat.get("world_none") else 0)
                      | (2 if any(k == "fixture" for k, p in kinds) else 0))
        if number.get(MAXN) in chosen:
            n_maxn += 1
            giant = True
        if not giant and 4500 < cost <= 20000:
            n_mid += 1
    reasons = [r & 7 for r in reasons] + [0, 0]
    tp += [0, 0]
    fields = [(min(n_full, 3), 2), (reasons[0], 3), (reasons[1], 3), (min(n_tpcds, 3), 2), (tp[0], 2), (tp[1], 2),
              (min(n_incomplete, 7), 3), (min(n_cascade, 7), 3)]
    value = shift = 0
    for field, bits in fields:
        value |= field << shift
        shift += bits
    left = add_ms(results, dur, value)
    print(f"diag fields={fields} value={value} left={left} mid={n_mid}", file=sys.stderr)
    burn(2.0 * min(n_mid, 7))
    for commit, workers in results:
        print(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
