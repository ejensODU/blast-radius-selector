"""Operand observations for ordinary comparison operators in the baseline test suite."""
from __future__ import annotations

import argparse
import ast
import builtins
import datetime
import decimal
import enum
import functools
import gzip
import importlib.machinery
import operator
import os
import pickle
import re
import sys
import types
from collections import defaultdict
from pathlib import Path

from impact import _source, apply_file

OPS = {'Eq': operator.eq, 'NotEq': operator.ne, 'Is': operator.is_, 'IsNot': operator.is_not,
       'Lt': operator.lt, 'LtE': operator.le, 'Gt': operator.gt, 'GtE': operator.ge}

_NONE_SAFE = (
    type(None), int, float, bool, str, tuple, dict, bytes, list, set, frozenset,
    datetime.date, datetime.datetime, datetime.time, datetime.timedelta,
    decimal.Decimal, re.Pattern, type,
    types.FunctionType, types.MethodType, types.ModuleType, types.BuiltinFunctionType,
)

_NUM_TYPES = (int, bool, float, complex, decimal.Decimal)
_DISJOINT_SAFE = (
    type(None), str, bytes, list, dict, set, frozenset, tuple,
    datetime.date, datetime.datetime, datetime.time, datetime.timedelta,
    re.Pattern, type, types.FunctionType, types.MethodType, types.ModuleType,
)

_EXPR_CLS = None


def _ordinary_slots(cls, base, names):
    """Inspect the first raw descriptor in the MRO without invoking it."""
    for name in names:
        descriptor = next((parent.__dict__[name] for parent in cls.__mro__
                           if name in parent.__dict__), None)
        if descriptor is not base.__dict__.get(name):
            return False
    return True


def _is_plain_expr(cls):
    global _EXPR_CLS
    if _EXPR_CLS is None:
        mod = sys.modules.get('sqlglot.expressions.core')
        if mod is not None:
            _EXPR_CLS = getattr(mod, 'Expression', None)
    if _EXPR_CLS is None or not isinstance(cls, type) or not issubclass(cls, _EXPR_CLS):
        return False
    return _ordinary_slots(cls, _EXPR_CLS, ('__eq__', '__ne__', '__hash__'))


class _PreHashed:
    __slots__ = ('_h',)
    def __init__(self, h):
        self._h = h
    def __hash__(self):
        return self._h


def _expr_hash_readonly(root, limit=256):
    """Compute sqlglot Expr.__hash__ without mutating any node._hash, bounded to `limit` nodes."""
    if root._hash is not None:
        return root._hash
    cache = {}
    stack = [(root, False)]
    visited = 0
    while stack:
        node, ready = stack.pop()
        nid = id(node)
        if nid in cache:
            continue
        if node._hash is not None:
            cache[nid] = node._hash
            continue
        if not ready:
            visited += 1
            if visited > limit:
                return None
            stack.append((node, True))
            for v in node.args.values():
                if isinstance(v, _EXPR_CLS):
                    if v._hash is not None:
                        cache[id(v)] = v._hash
                    elif id(v) not in cache:
                        stack.append((v, False))
                elif type(v) is list:
                    for x in v:
                        if isinstance(x, _EXPR_CLS):
                            if x._hash is not None:
                                cache[id(x)] = x._hash
                            elif id(x) not in cache:
                                stack.append((x, False))
            continue
        hash_ = hash(node.key)
        if node._hash_raw_args:
            for k in sorted(node.args):
                v = node.args[k]
                if v:
                    if type(v) is list:
                        tv = tuple(_PreHashed(cache[id(x)]) if isinstance(x, _EXPR_CLS) else x for x in v)
                        hash_ = hash((hash_, k, tv))
                    else:
                        hv = _PreHashed(cache[id(v)]) if isinstance(v, _EXPR_CLS) else v
                        hash_ = hash((hash_, k, hv))
        else:
            for k in sorted(node.args):
                v = node.args[k]
                vt = type(v)
                if vt is list:
                    for x in v:
                        if x is not None and x is not False:
                            hx = _PreHashed(cache[id(x)]) if isinstance(x, _EXPR_CLS) else (x.lower() if type(x) is str else x)
                            hash_ = hash((hash_, k, hx))
                        else:
                            hash_ = hash((hash_, k))
                elif v is not None and v is not False:
                    hv = _PreHashed(cache[id(v)]) if isinstance(v, _EXPR_CLS) else (v.lower() if vt is str else v)
                    hash_ = hash((hash_, k, hv))
        if not hash_:
            return None
        cache[nid] = hash_
    return cache.get(id(root))


# Observations store identity alongside the ordinary ordering relation. '?' means
# equality is known false and ordering is undefined; 'e' means equality is known
# true. None means opaque operands.
def relation(a, b, op=None, value=None):
    ta, tb = type(a), type(b)
    if a is None and b is None:
        return True, 'e'
    if a is None or b is None:
        other = b if a is None else a
        to = tb if a is None else ta
        if (to in _NONE_SAFE
                or (isinstance(other, type) and _ordinary_slots(to, object, ('__eq__', '__ne__')))
                or _is_plain_expr(to)
                or (isinstance(other, enum.Enum) and (
                    _ordinary_slots(to, object, ('__eq__', '__ne__'))
                    or (isinstance(other, str) and _ordinary_slots(to, str, ('__eq__', '__ne__')))
                ))
                or _ordinary_slots(to, object, ('__eq__', '__ne__'))):
            return False, '?'
        return None
    if isinstance(a, type) and isinstance(b, type):
        if (_ordinary_slots(ta, object, ('__eq__', '__ne__'))
                and _ordinary_slots(tb, object, ('__eq__', '__ne__'))):
            return a is b, 'e' if a is b else '?'
        return None
    if _is_plain_expr(ta) and _is_plain_expr(tb):
        if a is b:
            return True, 'e'
        if ta is not tb:
            return False, '?'
        if op == 'Eq':
            return False, 'e' if value else '?'
        if op == 'NotEq':
            return False, '?' if value else 'e'
        try:
            ha = _expr_hash_readonly(a)
            if ha is None:
                return None
            hb = _expr_hash_readonly(b)
        except Exception:
            return None
        if hb is None:
            return None
        return False, 'e' if ha == hb else '?'
    if (ta is int or ta is bool) and (tb is int or tb is bool):
        return a is b, ('=' if a == b else '<' if a < b else '>')
    if ta is tb is str:
        return a is b, ('=' if a == b else '<' if a < b else '>')
    if ta is tb is tuple:
        if (len(a) > 8 or len(b) > 8 or
                not all(type(v) is int for v in a) or not all(type(v) is int for v in b)):
            return None
        return a is b, ('=' if a == b else '<' if a < b else '>')
    if (all(any(base is str for base in t.__mro__) for t in (ta, tb))
            and all(_ordinary_slots(t, str, ('__eq__', '__ne__', '__lt__', '__le__', '__gt__', '__ge__'))
                    for t in (ta, tb))):
        return a is b, ('=' if a == b else '<' if a < b else '>')
    if (isinstance(a, enum.Enum) and isinstance(b, enum.Enum)
            and _ordinary_slots(ta, object, ('__eq__', '__ne__'))
            and _ordinary_slots(tb, object, ('__eq__', '__ne__'))):
        return a is b, 'e' if a is b else '?'
    if _ordinary_slots(ta, object, ('__eq__', '__ne__')) and _ordinary_slots(tb, object, ('__eq__', '__ne__')):
        return a is b, 'e' if a is b else '?'
    # Disjoint known types that never compare equal (e.g. Expr vs bool/str/int, str vs int/bool)
    if ta is not tb:
        a_expr, b_expr = _is_plain_expr(ta), _is_plain_expr(tb)
        a_safe = a_expr or ta in _DISJOINT_SAFE or ta in (int, bool)
        b_safe = b_expr or tb in _DISJOINT_SAFE or tb in (int, bool)
        if a_safe and b_safe and not (ta in _NUM_TYPES and tb in _NUM_TYPES):
            if not (isinstance(a, str) and isinstance(b, str)):
                if not (isinstance(a, datetime.date) and isinstance(b, datetime.date)):
                    return False, '?'
    return None


def interpreted(name, state):
    if state is None:
        return None
    ident, order = state
    if name == 'Is':
        return ident
    if name == 'IsNot':
        return not ident
    if name == 'Eq':
        return order in ('=', 'e')
    if name == 'NotEq':
        return order not in ('=', 'e')
    if order in ('?', 'e'):
        return None
    return order in {'Lt': '<', 'LtE': '<=', 'Gt': '>', 'GtE': '>='}[name]


class Instrument(ast.NodeTransformer):
    def __init__(self, path, sites):
        self.path, self.sites = path, sites

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]).__name__ not in OPS:
            return node
        rhs = node.comparators[0]
        position = (self.path, node.lineno, node.col_offset)
        site_num = len(self.sites)
        self.sites.append(position)
        key = type(node.ops[0]).__name__
        call = ast.Call(ast.Name('_blast_baseline_compare', ast.Load()),
                        [ast.Constant(site_num), ast.Constant(key), node.left, rhs], [])
        return ast.copy_location(call, node)


class SourceLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, name, path, recorder):
        super().__init__(name, path)
        self.recorder = recorder

    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = ast.parse(source, self.path)
        path = Path(self.path).relative_to(self.recorder.root).as_posix()
        tree = Instrument(path, self.recorder.sites).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, 'exec', dont_inherit=True)


class OperandRecorder:
    def __init__(self, root):
        self.root = root
        self.prefix = str(root).rstrip('/') + '/sqlglot/'
        self.strip = len(str(root).rstrip('/')) + 1
        self.sites = []
        self.active = False
        self.in_observe = False
        self.mask = 0
        self.seen = set()
        self.current = set()
        self.current_lines = set()
        self.observed = defaultdict(dict)
        self.line_seen = defaultdict(dict)
        self.ids = []
        self.numbers = {}
        self.groups = defaultdict(int)
        self.incomplete = 0
        self.mon = sys.monitoring
        self.tool_id = self.mon.COVERAGE_ID
        self.mon.use_tool_id(self.tool_id, 'values_lines')
        self.mon.register_callback(self.tool_id, self.mon.events.LINE, self._on_line)

    def _on_line(self, code, line):
        fn = code.co_filename
        if fn.startswith(self.prefix):
            self.current_lines.add((fn[self.strip:], line))
        return self.mon.DISABLE

    def find_spec(self, fullname, path, target=None):
        if fullname != 'sqlglot' and not fullname.startswith('sqlglot.'):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if (spec and spec.origin and spec.origin.endswith('.py')
                and Path(spec.origin).is_relative_to(self.root)):
            spec.loader = SourceLoader(fullname, spec.origin, self)
        return spec

    def observe(self, site, op, a, b):
        if self.in_observe:
            return OPS[op](a, b)
        self.in_observe = True
        try:
            try:
                value = OPS[op](a, b)
            except BaseException:
                if self.active:
                    self.current.add((site, None))
                raise
            if self.active:
                state = relation(a, b, op, value) if type(value) is bool else None
                self.current.add((site, state))
            return value
        finally:
            self.in_observe = False

    def flush(self, new_mask, track_lines=False):
        self.mon.set_events(self.tool_id, 0)
        for site, state in self.current:
            pos = self.sites[site]
            row = self.observed[pos]
            row[state] = row.get(state, 0) | self.mask
        self.current.clear()
        if self.current_lines:
            for path, line in self.current_lines:
                row = self.line_seen[path]
                row[line] = row.get(line, 0) | self.mask
            self.current_lines.clear()
        self.mask = new_mask
        self.active = bool(new_mask)
        if track_lines:
            self.mon.restart_events()
            self.mon.set_events(self.tool_id, self.mon.events.LINE)

    def pytest_collection_finish(self, session):
        self.ids = [i.nodeid for i in session.items]
        self.numbers = {name: i for i, name in enumerate(self.ids)}
        for i, name in enumerate(self.ids):
            self.groups[name.rpartition('::')[0]] |= 1 << i
        # Imports during collection can cache results consumed in any test.
        self.mask = (1 << len(self.ids)) - 1
        self.flush(0, track_lines=False)

    def pytest_runtest_setup(self, item):
        group = item.nodeid.rpartition('::')[0]
        if group not in self.seen:
            self.flush(self.groups[group], track_lines=False)
            self.seen.add(group)
        else:
            self.flush(1 << self.numbers[item.nodeid], track_lines=False)

    def pytest_runtest_call(self, item):
        self.flush(1 << self.numbers[item.nodeid], track_lines=True)

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.incomplete |= 1 << self.numbers[report.nodeid]

    def pytest_sessionfinish(self, session, exitstatus):
        self.flush(0, track_lines=False)
        self.mon.free_tool_id(self.tool_id)


@functools.lru_cache(maxsize=1)
def _load():
    try:
        with gzip.open(Path(__file__).with_name('values.pkl.gz'), 'rb') as data:
            return pickle.load(data)
    except FileNotFoundError:
        return None


def _matching_op(tree, lines):
    start, end = min(lines), max(lines)
    pending = [tree]
    while pending:
        node = pending.pop()
        if hasattr(node, 'lineno') and (node.end_lineno < start or node.lineno > end):
            continue
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            yield node
        pending.extend(ast.iter_child_nodes(node))


def _single_operator(path, root, patch):
    source, baseline, _ = _source(root / path)
    updated, changes = apply_file(source, patch)
    if len(changes) != 1 or len(changes[0].before) != len(changes[0].after):
        return None
    block = changes[0]
    lines = range(block.line, block.line + len(block.before))
    for prev in _matching_op(baseline, lines):
        if type(prev.ops[0]).__name__ not in OPS:
            continue
        old_lines = source.splitlines()
        left, right = prev.left, prev.comparators[0]
        if left.end_lineno != right.lineno or left.end_lineno != block.line or len(block.before) != 1:
            continue
        raw = old_lines[block.line - 1].encode()
        old_l = raw[:left.end_col_offset]
        old_r = raw[right.col_offset:]
        replacement = block.after[0].rstrip('\r\n').encode()
        if not replacement.startswith(old_l) or not replacement.endswith(old_r):
            continue
        end = len(replacement) - len(old_r)
        if end <= len(old_l):
            continue
        middle = b' '.join(replacement[len(old_l):end].split())
        syntax = {b'==': 'Eq', b'!=': 'NotEq', b'is': 'Is', b'is not': 'IsNot',
                  b'<': 'Lt', b'<=': 'LtE', b'>': 'Gt', b'>=': 'GtE'}
        if middle not in syntax:
            continue
        tree = ast.parse(updated)
        same = next((n for n in _matching_op(tree, lines)
                     if (n.lineno, n.col_offset) == (prev.lineno, prev.col_offset)
                     and type(n.ops[0]).__name__ == syntax[middle]
                     and ast.dump(n.left) == ast.dump(left)
                     and ast.dump(n.comparators[0]) == ast.dump(right)), None)
        if same:
            return (path, prev.lineno, prev.col_offset), type(prev.ops[0]).__name__, syntax[middle]
    return None


def _literal_replacement(path, root, patch):
    """Match a change confined to one literal RHS in an ordinary comparison."""
    source, tree, _ = _source(root / path)
    updated, changes = apply_file(source, patch)
    if len(changes) != 1:
        return None
    block = changes[0]
    if len(block.before) != 1 or len(block.after) != 1:
        return None
    line = block.line
    updated_tree = None
    for prev in _matching_op(tree, (line,)):
        rhs = prev.comparators[0]
        op = type(prev.ops[0]).__name__
        if rhs.lineno != rhs.end_lineno or rhs.lineno != line or op not in OPS or op.startswith('Is'):
            continue
        if not isinstance(rhs, (ast.Constant, ast.Tuple)):
            continue
        before, after = block.before[0].encode(), block.after[0].encode()
        if not after.startswith(before[:rhs.col_offset]) or not after.endswith(before[rhs.end_col_offset:]):
            continue
        if updated_tree is None:
            updated_tree = ast.parse(updated)
        for new in _matching_op(updated_tree, (line,)):
            if (new.lineno, new.col_offset) != (prev.lineno, prev.col_offset) or type(new.ops[0]).__name__ != op:
                continue
            new_rhs = new.comparators[0]
            if (ast.dump(new.left) != ast.dump(prev.left)
                    or (new_rhs.lineno, new_rhs.col_offset) != (rhs.lineno, rhs.col_offset)
                    or new_rhs.end_lineno != rhs.end_lineno
                    or after[new_rhs.end_col_offset:] != before[rhs.end_col_offset:]):
                continue
            try:
                a, b = ast.literal_eval(rhs), ast.literal_eval(new_rhs)
            except (ValueError, TypeError):
                continue
            if type(a) is not type(b) or type(a) not in (int, str, tuple):
                continue
            if type(a) is tuple and not all(type(v) is int for t in (a, b) for v in t):
                continue
            return (path, prev.lineno, prev.col_offset), op, a, b
    return None


def _literal_unchanged(op, state, old_rhs, new_rhs):
    if state is None or state[1] not in ('<', '=', '>'):
        return False
    order = state[1]
    if old_rhs == new_rhs:
        return True
    shift = '<' if old_rhs < new_rhs else '>'
    if order in ('=', shift):
        possible = shift
    elif type(old_rhs) is int and abs(old_rhs - new_rhs) == 1:
        possible = '>=' if shift == '<' else '<='
    else:
        possible = '<=>'
    return all(interpreted(op, (False, value)) == interpreted(op, state) for value in possible)


_NESTED_COMPARE_SCOPES = frozenset({
    "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__hash__",
})


def refine(impact, source_root, filepatch, mapdata, profile=None):
    """Return tests that may observe a change to this comparison, or None."""
    if impact.kind != 'body':
        return None
    # Nested comparisons inside dunder comparison/hash hooks or _Dialect are suppressed
    # during recording (observe() skips re-entrant calls while line_seen still marks the line).
    if impact.scope and (
        impact.scope[0] == "_Dialect"
        or any(s in _NESTED_COMPARE_SCOPES for s in impact.scope)
    ):
        return None
    if profile is None:
        profile = _load()
    if not profile or profile['ids'] != mapdata['ids']:
        return None
    operation = _single_operator(impact.path, source_root, filepatch)
    literal = None if operation else _literal_replacement(impact.path, source_root, filepatch)
    if operation:
        pos, old_op, new_op = operation
    elif literal:
        pos, op, old_rhs, new_rhs = literal
    else:
        return None
    row = profile['observed'].get(pos)
    line_seen = profile.get('line_seen', {}).get(pos[0], {}).get(pos[1], 0)
    if not row and not line_seen:
        return None
    seen = mask = 0
    for state, tests in (row or {}).items():
        seen |= tests
        if literal:
            if not _literal_unchanged(op, state, old_rhs, new_rhs):
                mask |= tests
        elif state is None:
            mask |= tests
        else:
            before = interpreted(old_op, state)
            after = interpreted(new_op, state)
            if before is None or after is None or before != after:
                mask |= tests
    # Preserve baseline contexts in which the line did not run during operand
    # recording (isolated-only cache/setup effects). Tests that ran the line
    # during their call (line_seen) without reaching this comparison short-circuited.
    known = seen | line_seen
    return mask | profile['incomplete'] | (((1 << len(profile['ids'])) - 1) ^ known)


def main():
    import concurrent.futures
    import pytest
    from build_map import InlinePool

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('tests', nargs='*')
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    os.chdir(root)
    os.environ['SKIP_INTEGRATION'] = '0'
    sys.path.insert(0, str(root))
    rec = OperandRecorder(root)
    builtins._blast_baseline_compare = rec.observe
    sys.meta_path.insert(0, rec)
    rec.active = True
    concurrent.futures.ProcessPoolExecutor = InlinePool
    OperandRecorder.pytest_runtest_setup = pytest.hookimpl(tryfirst=True)(OperandRecorder.pytest_runtest_setup)
    OperandRecorder.pytest_runtest_call = pytest.hookimpl(tryfirst=True)(OperandRecorder.pytest_runtest_call)
    code = pytest.main(['-q', '--disable-warnings', '--tb=short', '-p', 'no:cacheprovider',
                        '--continue-on-collection-errors', *(args.tests or ['tests'])], plugins=[rec])
    if not rec.ids or code not in (0, 1):
        raise SystemExit(f'Could not profile suite (pytest status {code})')
    with gzip.GzipFile(filename=str(output), mode='wb', compresslevel=6, mtime=0) as out:
        pickle.dump(dict(ids=rec.ids, observed=dict(rec.observed),
                         line_seen={p: dict(v) for p, v in rec.line_seen.items()},
                         incomplete=rec.incomplete), out, protocol=5)
    print(f'sites={len(rec.sites)} observed={len(rec.observed)} incomplete={bin(rec.incomplete).count("1")}', flush=True)


if __name__ == '__main__':
    main()
