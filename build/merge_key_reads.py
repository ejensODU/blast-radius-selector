"""Merge per-test read logs into cell -> {key token -> test mask}, '*' for whole-dict reads."""
import gzip, json, pickle, sys
from collections import defaultdict
base = pickle.load(gzip.open(sys.argv[1], 'rb'))            # the line map, for test numbering
number = {name: i for i, name in enumerate(base['ids'])}
ids = json.load(open('track/ids.json'))
reads = defaultdict(dict); imports = defaultdict(int); wrapped = set(); alias = {}; untracked = 0; seconds = {}
for i, nodeid in enumerate(ids):
    bit = 1 << number[nodeid]
    try:
        r = json.load(open(f'track/{i}.json'))
    except FileNotFoundError:
        untracked |= bit; continue
    if r['outcome'] != 'ok':
        untracked |= bit
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
    for path in r['imports']:
        imports[path] |= bit
out = {'ids': base['ids'], 'reads': dict(reads), 'imports': dict(imports), 'wrapped': wrapped, 'alias': alias,
       'untracked': untracked, 'seconds': seconds}
with gzip.GzipFile(sys.argv[2], 'wb', compresslevel=6, mtime=0) as f:
    pickle.dump(out, f, protocol=5)
print('cells with reads', len(reads), 'wrapped', len(wrapped), 'aliases', len(alias), 'untracked tests', bin(untracked).count('1'),
      'keys', sum(len(v) for v in reads.values()))
