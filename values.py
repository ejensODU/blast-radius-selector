"""Operand observations for ordinary comparison operators in the baseline test suite."""
from __future__ import annotations

import argparse
import ast
import builtins
import enum
import functools
import gzip
import importlib.machinery
import operator
import os
import pickle
import sys
from collections import defaultdict
from pathlib import Path

from impact import _source, apply_file

OPS = {'Eq': operator.eq, 'NotEq': operator.ne, 'Is': operator.is_, 'IsNot': operator.is_not,
       'Lt': operator.lt, 'LtE': operator.le, 'Gt': operator.gt, 'GtE': operator.ge}
def _ordinary_slots(cls, base, names):
    """Inspect the first raw descriptor in the MRO without invoking it."""
    for name in names:
        descriptor = next((parent.__dict__[name] for parent in cls.__mro__
                           if name in parent.__dict__), None)
        if descriptor is not base.__dict__[name]:
            return False
    return True


# Observations store identity alongside the ordinary ordering relation. '?' means
# equality is known false but ordering is undefined; None means opaque operands.
def relation(a, b):
    ta, tb = type(a), type(b)
    if (type(ta) is not type and type(ta) is not enum.EnumType
            or type(tb) is not type and type(tb) is not enum.EnumType):
        return None
    if a is None and b is None:
        return True, 'e'
    if ta is tb is tuple:
        # Glot version and other fixed tuples of primitive numbers.
        if (len(a) > 8 or len(b) > 8 or
                not all(type(v) is int for v in a) or not all(type(v) is int for v in b)):
            return None
    elif (ta is int or ta is bool) and (tb is int or tb is bool):
        pass
    elif ta is tb is str:
        pass
    elif a is not None and b is not None and all(any(base is str for base in t.__mro__) for t in (ta, tb)):
        # A str-valued Enum is common. Require ordinary base string semantics.
        if not all(_ordinary_slots(t, str, ('__eq__', '__ne__', '__lt__', '__le__', '__gt__', '__ge__'))
                   for t in (ta, tb)):
            return None
    elif (a is None or b is None) and (
        any((tb if a is None else ta) is t
            for t in (type(None), int, float, bool, str, tuple, dict, bytes, list, set))
        or _ordinary_slots(tb if a is None else ta, object, ('__eq__', '__ne__'))
    ):
        return a is b, 'e' if a is b else '?'
    elif all(_ordinary_slots(t, object, ('__eq__', '__ne__')) for t in (ta, tb)):
        return a is b, 'e' if a is b else '?'
    else:
        return None
    return a is b, ('=' if a == b else '<' if a < b else '>')


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
        if not (isinstance(rhs, (ast.Constant, ast.Tuple))
                or (isinstance(rhs, ast.Attribute) and rhs.attr.isupper())):
            return node
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
        self.sites = []
        self.active = False
        self.mask = 0
        self.seen = set()
        self.current = set()
        self.observed = defaultdict(dict)
        self.ids = []
        self.numbers = {}
        self.groups = defaultdict(int)
        self.incomplete = 0

    def find_spec(self, fullname, path, target=None):
        if fullname != 'sqlglot' and not fullname.startswith('sqlglot.'):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if (spec and spec.origin and spec.origin.endswith('.py')
                and Path(spec.origin).is_relative_to(self.root)):
            spec.loader = SourceLoader(fullname, spec.origin, self)
        return spec

    def observe(self, site, op, a, b):
        try:
            value = OPS[op](a, b)
        except BaseException:
            if self.active:
                self.current.add((site, None))
            raise
        if self.active:
            state = relation(a, b) if type(value) is bool else None
            self.current.add((site, state))
        return value

    def flush(self, new_mask):
        for site, state in self.current:
            pos = self.sites[site]
            row = self.observed[pos]
            row[state] = row.get(state, 0) | self.mask
        self.current.clear()
        self.mask = new_mask
        self.active = bool(new_mask)

    def pytest_collection_finish(self, session):
        self.ids = [i.nodeid for i in session.items]
        self.numbers = {name: i for i, name in enumerate(self.ids)}
        for i, name in enumerate(self.ids):
            self.groups[name.rpartition('::')[0]] |= 1 << i
        # Imports during collection can cache results consumed in any test.
        self.mask = (1 << len(self.ids)) - 1
        self.flush(0)

    def pytest_runtest_setup(self, item):
        group = item.nodeid.rpartition('::')[0]
        if group not in self.seen:
            self.flush(self.groups[group])
            self.seen.add(group)
        else:
            self.flush(1 << self.numbers[item.nodeid])

    def pytest_runtest_call(self, item):
        self.flush(1 << self.numbers[item.nodeid])

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.incomplete |= 1 << self.numbers[report.nodeid]

    def pytest_sessionfinish(self, session, exitstatus):
        self.flush(0)


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
        # For the precise candidate, require that the only token changed in the
        # entire source is the operator between its two original operands.
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
        # Require actual replacement to produce a valid comparison with the
        # same grouped operands (operator precedence around "is not", etc.).
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
        # Only exact integer and boolean operands get ordered with an int RHS.
        possible = '>=' if shift == '<' else '<='
    else:
        possible = '<=>'
    return all(interpreted(op, (False, value)) == interpreted(op, state) for value in possible)


def refine(impact, source_root, filepatch, mapdata):
    """Return tests that may observe a change to this comparison, or None."""
    if impact.kind != 'body':
        return None
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
    if not row:
        return None
    seen = mask = 0
    for state, tests in row.items():
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
    # Preserve baseline contexts in which the line ran but the separate
    # operand-recording run did not observe it (cache order and setup effects).
    return mask | profile['incomplete'] | (((1 << len(profile['ids'])) - 1) ^ seen)


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
        pickle.dump(dict(ids=rec.ids, observed=dict(rec.observed), incomplete=rec.incomplete), out, protocol=5)


if __name__ == '__main__':
    main()
