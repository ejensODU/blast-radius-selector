"""Measure the CPU seconds a fresh-interpreter probe spends on each test alone.

usage: probe_cost_run.py <sqlglot root> <tests.json> <out.json> [jobs]
Runs /app/probe.py exactly as the selector does (same child command), one test per
child, against the pristine tree, and records user+system CPU of the child tree.
"""
import json, os, resource, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

root, tests_json, dest = sys.argv[1], sys.argv[2], sys.argv[3]
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 4
ids = [t["id"] for t in json.load(open(tests_json))["tests"]]
env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=root)


def one(nodeid):
    # A wrapper child reports its own children's CPU, so parallel jobs do not mix.
    code = ("import json,resource,subprocess,sys\n"
            "p=subprocess.run([sys.executable,'-B','/app/probe.py','tests'],input=sys.stdin.buffer.read(),capture_output=True,cwd=%r)\n"
            "r=resource.getrusage(resource.RUSAGE_CHILDREN)\n"
            "print(json.dumps([r.ru_utime+r.ru_stime,p.stdout.decode()]))\n" % root)
    out = subprocess.run([sys.executable, "-c", code], input=json.dumps([nodeid]).encode(), capture_output=True, env=env)
    try:
        cpu, text = json.loads(out.stdout)
    except ValueError:
        return nodeid, None, False
    return nodeid, cpu, nodeid in text


result = {}
start = time.time()
with ThreadPoolExecutor(jobs) as pool:
    for i, (nodeid, cpu, ok) in enumerate(pool.map(one, ids + ["tests/none.py::None::none"])):
        result[nodeid] = [cpu, ok]
        if i % 200 == 0:
            print(i, round(time.time() - start), flush=True)
json.dump(result, open(dest, "w"))
print("done", round(time.time() - start), flush=True)
