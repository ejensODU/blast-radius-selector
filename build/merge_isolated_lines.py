"""Union per-test isolated traces (out/<i>.pkl.gz) into Astra's single-process map."""
import gzip, json, pickle, sys
from pathlib import Path
base_path, out_dir, dest = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
base = pickle.load(gzip.open(base_path, 'rb'))
ids = json.load(open(out_dir / 'ids.json'))
number = {name: i for i, name in enumerate(base['ids'])}
def cnt(m): return bin(m).count('1')
before = sum(cnt(m) for hits in base['lines'].values() for m in hits.values())
missing, iso_incomplete, unknown = [], [], []
for i, nodeid in enumerate(ids):
    f = out_dir / f'{i}.pkl.gz'
    if not f.exists():
        missing.append(nodeid); continue
    if nodeid not in number:
        unknown.append(nodeid); continue
    extra = pickle.load(gzip.open(f, 'rb'))
    if extra['ids'] != [nodeid]:
        missing.append(nodeid); continue
    if extra['incomplete'] & 1:
        iso_incomplete.append(nodeid); continue
    bit = 1 << number[nodeid]
    for group in ('reads', 'classes', 'names', 'modules'):
        d = base[group]
        for key, mask in extra[group].items():
            if mask & 1:
                d[key] = d.get(key, 0) | bit
    for filename, hits in extra['lines'].items():
        lines = base['lines'].setdefault(filename, {})
        for line, mask in hits.items():
            if mask & 1:
                lines[line] = lines.get(line, 0) | bit
after = sum(cnt(m) for hits in base['lines'].values() for m in hits.values())
with gzip.GzipFile(filename=dest, mode='wb', compresslevel=6, mtime=0) as out:
    pickle.dump(base, out, protocol=5)
print(f'(line,test) pairs: {before} -> {after} (+{after-before})')
print(f'missing traces: {len(missing)}; failed/skipped in isolation: {len(iso_incomplete)}; unknown ids: {len(unknown)}')
for name in (missing[:10] + iso_incomplete[:20]): print('  ', name)
json.dump({'missing': missing, 'iso_incomplete': iso_incomplete}, open(Path(dest).with_suffix('.report.json'), 'w'), indent=1)
