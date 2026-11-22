"""Run slice_one.py for the set-up and every element of each fixture-driven test."""
import json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
root, out, jobs = sys.argv[1], sys.argv[2], int(sys.argv[3])
TESTS = [("tests/test_executor.py::TestExecutor::test_execute_tpcds", "tpcds_sqls", 99),
         ("tests/test_executor.py::TestExecutor::test_execute_tpch", "tpch_sqls", 22),
         ("tests/test_executor.py::TestExecutor::test_optimized_tpch", "tpch_sqls", 22)]
work = []
for test, attr, size in TESTS:
    name = test.split("::")[-1]
    for which in ["setup", *map(str, range(size))]:
        work.append((test, attr, which, f"{out}/{name}.{which}.json"))
def one(item):
    test, attr, which, dest = item
    r = subprocess.run([sys.executable, "/v3/slice_one.py", root, test, attr, which, dest], capture_output=True, text=True)
    return dest, r.returncode, r.stderr[-300:]
start = time.time()
with ThreadPoolExecutor(jobs) as pool:
    for i, (dest, rc, err) in enumerate(pool.map(one, work)):
        if rc: print("FAILED", dest, err, flush=True)
        if i % 20 == 0: print(i, round(time.time() - start), flush=True)
print("done", round(time.time() - start), flush=True)
