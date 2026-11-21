import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
tests = json.load(open('/data/tests.json'))
ids = [t['id'] for t in tests['tests']] + list(tests.get('skipped', []))
workers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
def run(item):
    i, nodeid = item
    dest = f'/out/{i}.json'
    if os.path.exists(dest):
        return 0
    try:
        r = subprocess.run([sys.executable, '/w/track_one.py', '/opt/sqlglot', nodeid, dest + '.tmp'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1500)
        rc = r.returncode
        err = r.stderr[-3000:]
    except subprocess.TimeoutExpired:
        rc, err = -9, b'timeout'
    if rc == 0 and os.path.exists(dest + '.tmp'):
        os.replace(dest + '.tmp', dest)
        return 0
    open(f'/out/{i}.err', 'wb').write(err)
    return 1
json.dump(ids, open('/out/ids.json', 'w'))
t0 = time.time(); done = bad = 0
with ThreadPoolExecutor(workers) as pool:
    for rc in pool.map(run, list(enumerate(ids))):
        done += 1; bad += rc
        if done % 200 == 0:
            print(f'{done}/{len(ids)} bad={bad} elapsed={time.time()-t0:.0f}s', flush=True)
print(f'finished {done} bad={bad} elapsed={time.time()-t0:.0f}s', flush=True)
