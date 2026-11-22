"""Merge per-test v2 read logs into cell -> {key token -> test mask}, plus scalar attr_reads."""
import gzip, json, pickle, sys
from collections import defaultdict

base = pickle.load(gzip.open(sys.argv[1], 'rb'))
number = {name: i for i, name in enumerate(base['ids'])}
track_dir = sys.argv[2]
dest = sys.argv[3]
ids = json.load(open(f'{track_dir}/ids.json'))

reads = defaultdict(dict)
imports = defaultdict(int)
wrapped = set()
alias = {}
attr_reads = defaultdict(int)
attr_wrapped = set()
untracked = 0
seconds = {}
bad_outcomes = []

for i, nodeid in enumerate(ids):
    bit = 1 << number[nodeid]
    try:
        r = json.load(open(f'{track_dir}/{i}.json'))
    except FileNotFoundError:
        untracked |= bit
        continue
    if r['outcome'] != 'ok':
        untracked |= bit
        bad_outcomes.append((nodeid, r['outcome'], r.get('detail', '')[:120]))
    seconds[nodeid] = r['seconds']
    cells = [tuple(c) for c in r['cells']]
    wrapped.update(cells)
    for a, b in r['aliases']:
        alias[tuple(a)] = cells[b]
    for c, toks in r['reads'].items():
        row = reads[cells[int(c)]]
        for tok in toks:
            row[tok] = row.get(tok, 0) | bit
    for c in r['bulk']:
        row = reads[cells[c]]
        row['*'] = row.get('*', 0) | bit
    acells = [tuple(c) for c in r.get('attr_cells', ())]
    attr_wrapped.update(acells)
    for c in r.get('attr_reads', ()):
        attr_reads[acells[c]] |= bit
    for path in r['imports']:
        imports[path] |= bit

out = {
    'ids': base['ids'],
    'reads': dict(reads),
    'imports': dict(imports),
    'wrapped': wrapped,
    'alias': alias,
    'attr_reads': dict(attr_reads),
    'attr_wrapped': attr_wrapped,
    'untracked': untracked,
    'seconds': seconds,
}
with gzip.GzipFile(dest, 'wb', compresslevel=6, mtime=0) as f:
    pickle.dump(out, f, protocol=5)

print(f'cells_with_reads={len(reads)} wrapped={len(wrapped)} aliases={len(alias)} '
      f'attr_wrapped={len(attr_wrapped)} attr_reads={len(attr_reads)} '
      f'untracked={bin(untracked).count("1")} keys={sum(len(v) for v in reads.values())}')
for nid, oc, det in bad_outcomes[:10]:
    print('  non-ok:', nid, oc, det)
