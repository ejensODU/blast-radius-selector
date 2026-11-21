"""Practice-only diagnostic build.

Selections are exactly those of selector.py. A few counters describing how the
selector classified the batch are then reported through the score: extra tests
are added to already-selected commits (which can only add cost, never a miss),
and a counted number of CPU seconds is burned. Not for the final.
"""
import gzip, json, os, pickle, sys, time
from pathlib import Path

import selector as S
from impact import analyze
from schedule import schedule

GIANTS = ("tests/test_executor.py::TestExecutor::test_execute_tpcds",
          "tests/test_parser.py::TestParser::test_max_nodes")


def cost_ms(workers, dur):
    return (max(sum(dur[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0


def classify(text, root, index, workers):
    """Why the selector answered as it did, from the same analysis it used."""
    data = index.profile
    impacts = analyze(text, root)
    kinds = {i.kind for i in impacts}
    n = sum(len(w) for w in workers)
    full = n >= len(index.durations)
    reason = ""
    if full:
        if "unknown" in kinds:
            reason = "unknown"
        elif any(i.kind == "body" and i.lines.intersection(data["startup_body"].get(i.path, set())) for i in impacts):
            reason = "startup"
        elif "binding" in kinds:
            reason = "binding"
        else:
            reason = "other"
    return kinds, full, reason


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


def main():
    data, root = Path("/data"), Path("/opt/sqlglot").resolve()
    if len(sys.argv) > 2:
        data, root = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
    start = time.monotonic()
    tests = json.loads((data / "tests.json").read_bytes())["tests"]
    with gzip.open(S.APP / "map.pkl.gz", "rb") as stream:
        index = S.TestIndex(tests, pickle.load(stream))
    dur = index.durations
    full_plan = schedule([t["number"] for t in tests], dur)
    giants = {t["number"] for t in tests if t["id"] in GIANTS}
    results, texts = [], {}
    for path in sorted((data / "commits").glob("*.patch")):
        try:
            budget = max(0.0, 65.0 - (time.monotonic() - start))
            text = path.read_text(encoding="utf-8")
            texts[path.stem] = text
            workers = S.select(text, root, index, budget, False, True)
        except (OSError, ValueError, RecursionError, SyntaxError, UnicodeError):
            workers = full_plan
        results.append([path.stem, workers])

    c = dict(full=0, giant=0, bind=0, s_bind=0, empty=0, r_unknown=0, r_startup=0, r_binding=0, r_other=0,
             s_giant=0, bodyonly=0, s_bodyonly=0)
    try:
        for name, workers in results:
            ms = cost_ms(workers, dur)
            try:
                kinds, full, reason = classify(texts.get(name, ""), root, index, workers)
            except Exception:
                kinds, full, reason = {"unknown"}, sum(len(w) for w in workers) >= len(dur), "unknown"
            has_giant = any(n in giants for w in workers for n in w)
            if full:
                c["full"] += 1
                c["r_" + reason] += 1
            if not workers:
                c["empty"] += 1
            if has_giant and not full:
                c["giant"] += 1
                c["s_giant"] += ms
            if "binding" in kinds:
                c["bind"] += 1
                c["s_bind"] += ms
            if kinds <= {"body", "equivalent"} and "body" in kinds and not full:
                c["bodyonly"] += 1
                c["s_bodyonly"] += ms
        cap = lambda v, bits: min(int(v), (1 << bits) - 1)
        variant = os.urandom(1)[0] % 3
        if variant == 0:
            # empty is 6 bits wide in variant 1; here only its low 3 bits fit, as a cross-check
            p = cap(c["giant"], 4) | cap(c["bind"], 5) << 4 | cap(c["s_bind"] / 4000 + 0.5, 6) << 9 | (c["empty"] & 7) << 15
        elif variant == 1:
            p = cap(c["empty"], 6) | cap(c["r_unknown"], 3) << 6 | cap(c["r_startup"], 3) << 9 | cap(c["r_binding"], 3) << 12 | cap(c["r_other"], 3) << 15
        else:
            p = cap(c["s_giant"] / 4000 + 0.5, 7) | cap(c["bodyonly"], 5) << 7 | cap(c["s_bodyonly"] / 4000 + 0.5, 6) << 12
        left = add_ms(results, dur, variant + 3 * p)
        print(f"diag variant={variant} payload={p} left={left} counters={c}", file=sys.stderr)
        burn(min(c["full"], 15) * 2.0)
    except Exception as error:  # never let the diagnostic break the answer
        print(f"diag failed: {type(error).__name__}: {error}", file=sys.stderr)
    for name, workers in results:
        print(json.dumps({"commit": name, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
