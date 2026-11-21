"""Run Astra's map builder once per test, each in a fresh interpreter (inside the image)."""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
tests = json.load(open('/data/tests.json'))
ids = [t['id'] for t in tests['tests']] + list(tests.get('skipped', []))
out = '/out'
workers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
def run(item):
    i, nodeid = item
    dest = f'{out}/{i}.pkl.gz'
    if os.path.exists(dest):
        return i, 0, 0.0
    t0 = time.time()
    r = subprocess.run([sys.executable, '/app/build_map.py', '--single-attempt', '/opt/sqlglot', dest + '.tmp', nodeid],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
    if r.returncode == 0 and os.path.exists(dest + '.tmp'):
        os.replace(dest + '.tmp', dest)
    else:
        open(f'{out}/{i}.err', 'wb').write(r.stderr[-4000:])
    return i, r.returncode, time.time() - t0
json.dump(ids, open(f'{out}/ids.json', 'w'))
done = bad = 0
t0 = time.time()
with ThreadPoolExecutor(workers) as pool:
    for i, rc, dt in pool.map(run, list(enumerate(ids))):
        done += 1; bad += rc != 0
        if done % 100 == 0:
            print(f'{done}/{len(ids)} bad={bad} elapsed={time.time()-t0:.0f}s', flush=True)
print(f'finished {done} bad={bad} elapsed={time.time()-t0:.0f}s', flush=True)
