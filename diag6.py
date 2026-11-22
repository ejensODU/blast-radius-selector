"""Practice-only diagnostic for 5c9f9f7: identical selections plus encoded decision/CPU counters."""
import gzip
import json
import pickle
import sys
import time
from pathlib import Path

import selector as S
from schedule import schedule

TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


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
    if total > 0:
        for _, workers in results:
            if total <= 2000:
                break
            if workers:
                continue
            first = next((n for n in order if 2000 + dur[n] <= total), None)
            if first is None:
                continue
            workers.append([first])
            chosen = {first}
            total -= 2000 + dur[first]
            for n in order:
                if dur[n] <= total and n not in chosen:
                    workers[0].append(n)
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
    tpcds_num = number.get(TPCDS)
    full_plan = schedule([t["number"] for t in tests], dur)
    patches = sorted((data / "commits").glob("*.patch"))
    total_budget = 155.0 if len(patches) <= 45 else 235.0
    results, stats = [], []
    for path in patches:
        stat = {}
        try:
            budget = max(0.0, total_budget - (time.monotonic() - start))
            workers = S.select(path.read_text(encoding="utf-8"), root, index, budget, False, True)
            stat = dict(S.LAST)
        except (OSError, ValueError, RecursionError, SyntaxError, UnicodeError) as error:
            print(f"Conservative schedule for {path.stem}: {type(error).__name__}", file=sys.stderr)
            workers = full_plan
            stat = {"error": True}
        results.append((path.stem, [list(w) for w in workers]))
        stats.append(stat)

    n_tpcds = 0
    tp_reasons = []
    unprobed_non_tpcds_ms = 0
    n_unprobed_mid = 0
    tot_stepA = 0.0
    tot_stepBC = 0.0
    tot_BC_fail = 0.0
    any_cascade = 0
    for (_, workers), stat in zip(results, stats):
        chosen = {n for w in workers for n in w}
        cost = max(sum(dur[n] for n in w) for w in workers) + 2000 * len(workers) if workers else 0
        tot_stepA += float(stat.get("cpu_stepA", 0.0))
        tot_stepBC += float(stat.get("cpu_stepBC", 0.0))
        tot_BC_fail += float(stat.get("cpu_BC_fail", 0.0))
        if stat.get("cascade"):
            any_cascade = 1
        if tpcds_num in chosen:
            n_tpcds += 1
            tp_reasons.append(int(stat.get("tpcds_reason", 0)) & 3)
        elif cost > 0 and stat.get("probe_eligible") and not stat.get("full_probed") and not stat.get("test_code"):
            unprobed_non_tpcds_ms += cost
            if cost >= 4000:
                n_unprobed_mid += 1

    tp_reasons += [0, 0]
    fields = [
        (min(n_tpcds, 3), 2),
        (tp_reasons[0], 2),
        (tp_reasons[1], 2),
        (min(round(unprobed_non_tpcds_ms / 3000), 15), 4),
        (min(n_unprobed_mid, 7), 3),
        (min(round(tot_stepA / 2.0), 7), 3),
        (min(round(tot_stepBC / 2.0), 7), 3),
        (any_cascade, 1),
    ]
    value = shift = 0
    for field, bits in fields:
        value |= field << shift
        shift += bits
    left = add_ms(results, dur, value)
    burn_k = min(round(tot_BC_fail / 1.5), 7)
    print(
        f"diag6 fields={fields} value={value} left={left} "
        f"unprobed_ms={unprobed_non_tpcds_ms} stepA={tot_stepA:.2f}s stepBC={tot_stepBC:.2f}s "
        f"BC_fail={tot_BC_fail:.2f}s burn_k={burn_k}",
        file=sys.stderr,
    )
    burn(2.0 * burn_k)
    for commit, workers in results:
        print(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
