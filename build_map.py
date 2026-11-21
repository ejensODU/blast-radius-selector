"""Trace the unmodified sqlglot revision. Run with CPython 3.13 and requirements.lock."""
import argparse
import ast
from collections import defaultdict
import concurrent.futures as futures
import dis
import functools
import gzip
import importlib
import importlib.util
import inspect
import os
from pathlib import Path
import pickle
import sys
import subprocess
import tempfile

import pytest


class InlinePool(futures.Executor):
    """Keep optimizer and executor worker calls inside their test's trace context."""

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        future = futures.Future()
        try:
            # Spawn pools pickle both the task arguments and its return value.
            # SQLGlot defines its own expression serialization. Replaying that
            # boundary keeps tests that use it visible in the dependency map.
            func, values, options = pickle.loads(pickle.dumps((fn, args, kwargs)))
            result = func(*values, **options)
            future.set_result(pickle.loads(pickle.dumps(result)))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def module_map(root, ids):
    """Resolve import statements and implicit parent packages to test-file users."""
    files = {}
    for base in ('sqlglot', 'tests'):
        for path in (root / base).rglob('*.py'):
            name = path.relative_to(root).with_suffix('').as_posix().replace('/', '.')
            files[name.removesuffix('.__init__')] = path
    edges = defaultdict(set)
    for name, file in files.items():
        package = name if file.name == '__init__.py' else name.rpartition('.')[0]
        edges[name].add(name.rpartition('.')[0])
        try:
            tree = ast.parse(file.read_bytes())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                edges[name].update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ''
                if node.level:
                    try:
                        base = importlib.util.resolve_name('.' * node.level + base, package)
                    except ImportError:
                        continue
                edges[name].add(base)
                edges[name].update(f'{base}.{alias.name}' for alias in node.names)
    tests = defaultdict(int)
    for i, nodeid in enumerate(ids):
        tests[nodeid.split('::', 1)[0]] |= 1 << i
    modules = defaultdict(int)
    for filename, mask in tests.items():
        pending = [filename.removesuffix('.py').replace('/', '.')]
        seen = set()
        while pending:
            name = pending.pop()
            if name in seen or name not in files:
                continue
            seen.add(name)
            modules[files[name].relative_to(root).as_posix()] |= mask
            pending.extend(edges[name])
    return dict(modules)


def read_names(code):
    """Attribute/global loads, plus literal identifiers usable by getattr or mapping reads."""
    for op in dis.get_instructions(code):
        if (op.opname in ('LOAD_ATTR', 'LOAD_GLOBAL', 'LOAD_NAME', 'LOAD_METHOD', 'LOAD_SUPER_ATTR')
                or (op.opname == 'LOAD_CONST' and isinstance(op.argval, str) and op.argval.isidentifier())):
            start = op.positions.lineno
            if start is not None:
                yield op.argval, start, op.positions.end_lineno or start


class Recorder:
    TOOL = 4

    def __init__(self, root):
        self.root = root
        self.prefix = str(root) + '/'
        self.ids = []
        self.numbers = {}
        self.groups = defaultdict(int)
        self.incomplete = 0
        self.failures = set()
        self.mask = 0
        self.seen_classes = set()
        self.lines = defaultdict(dict)
        self.reads = defaultdict(int)
        self.classes = defaultdict(int)
        self.names = defaultdict(int)
        self.code_masks = {}  # keyed by code identity; identical bytecode may span files
        self.class_setup = set()

    def pytest_collection_finish(self, session):
        self.ids = [item.nodeid for item in session.items]
        self.numbers = {name: i for i, name in enumerate(self.ids)}
        for i, name in enumerate(self.ids):
            self.groups[name.rpartition('::')[0]] |= 1 << i
        self.start_monitor()

    def start_monitor(self):
        mon = sys.monitoring
        mon.use_tool_id(self.TOOL, 'sqlglot baseline test map')
        mon.register_callback(self.TOOL, mon.events.LINE, self.line)
        mon.register_callback(self.TOOL, mon.events.PY_START, self.start_code)
        mon.set_events(self.TOOL, mon.events.PY_START)
        sys.addaudithook(self.audit)

    def start_code(self, code, offset):
        # Keep import and class-body execution out of per-test runtime lines.
        if code.co_filename.startswith(self.prefix) and code.co_flags & inspect.CO_OPTIMIZED:
            if id(code) not in self.code_masks:
                relative = code.co_filename[len(self.prefix):]
                self.code_masks[id(code)] = (code, relative, {})
                sys.monitoring.set_local_events(self.TOOL, code, sys.monitoring.events.LINE)
        return sys.monitoring.DISABLE

    def line(self, code, line):
        if self.mask:
            hits = self.code_masks[id(code)][2]
            hits[line] = hits.get(line, 0) | self.mask
        return sys.monitoring.DISABLE

    def audit(self, event, args):
        if event != 'open' or not self.mask:
            return
        path = args[0]
        if isinstance(path, str):
            # Source-loader I/O is accounted for in the module import graph.
            # Keep intentional reads, such as tests that inspect source text.
            if path.endswith(('.pyc', '.pyo')) or sys._getframe(1).f_code.co_filename.startswith('<frozen importlib.'):
                return
            if not os.path.isabs(path):
                path = os.path.abspath(path)
            if path.startswith(self.prefix):
                self.reads[path[len(self.prefix):]] |= self.mask

    def set_mask(self, mask):
        for cls in self.seen_classes:
            for base in cls.__mro__:
                mod = sys.modules.get(base.__module__)
                filename = getattr(mod, '__file__', '') or ''
                if filename.startswith(self.prefix):
                    self.classes[(filename[len(self.prefix):], base.__qualname__)] |= self.mask
        self.seen_classes.clear()
        self.mask = mask
        sys.monitoring.restart_events()

    def install_receivers(self):
        # The same inherited tokenizer instance can be cached and reused by many tests.
        # Observe use as well as construction. Resolve MROs only once per test.
        from sqlglot import exp
        from sqlglot.dialects.dialect import Dialect
        from sqlglot.generator import Generator
        from sqlglot.parser import Parser
        from sqlglot.tokens import Tokenizer

        for cls, method in [(exp.Expr, '__init__'), (Dialect, '__init__'), (Dialect, 'tokenize'),
                            (Tokenizer, '__init__'), (Tokenizer, 'tokenize'), (Parser, '__init__'),
                            (Parser, 'parse'), (Parser, 'parse_into'), (Generator, '__init__'),
                            (Generator, 'generate')]:
            original = getattr(cls, method)

            @functools.wraps(original)
            def track(receiver, *args, _original=original, **kwargs):
                self.seen_classes.add(type(receiver))
                return _original(receiver, *args, **kwargs)

            setattr(cls, method, track)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        group = item.nodeid.rpartition('::')[0]
        if group not in self.class_setup:
            self.set_mask(self.groups[group])
            self.class_setup.add(group)
        else:
            self.set_mask(1 << self.numbers[item.nodeid])

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        current = 1 << self.numbers[item.nodeid]
        if self.mask != current:
            self.set_mask(current)

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failures.add(report.nodeid)
        if report.failed or report.skipped:
            self.incomplete |= 1 << self.numbers[report.nodeid]

    def pytest_sessionfinish(self, session, exitstatus):
        if self.ids:
            self.set_mask(0)
            sys.monitoring.set_events(self.TOOL, 0)
            for code, _, _ in self.code_masks.values():
                sys.monitoring.set_local_events(self.TOOL, code, 0)
            sys.monitoring.free_tool_id(self.TOOL)

    def result(self):
        for code, relative, hits in self.code_masks.values():
            lines = self.lines[relative]
            for line, mask in hits.items():
                lines[line] = lines.get(line, 0) | mask
            for name, first, last in read_names(code):
                # Multiline attribute positions can span the full receiver expression.
                mask = hits.get(first, 0) | hits.get(last, 0)
                if mask:
                    self.names[name] |= mask
        return {'ids': self.ids, 'lines': dict(self.lines), 'reads': dict(self.reads),
                'names': dict(self.names), 'classes': dict(self.classes),
                'modules': module_map(self.root, self.ids), 'incomplete': self.incomplete}


def startup_map(root, output):
    """Observe first-time module execution in a fresh interpreter.

    Pytest collection imports its test modules before the first test starts.
    Some dialects and optimizers are lazy. Import the remaining library modules
    so their registration code and any called factories are measured as well.
    """
    hits = defaultdict(set)
    body_hits = defaultdict(set)
    prefix = str(root) + '/'
    used_codes = {}

    def line(code, lineno):
        if code.co_filename.startswith(prefix):
            used_codes[id(code)] = code
            filename = code.co_filename[len(prefix):]
            hits[filename].add(lineno)
            if code.co_flags & inspect.CO_OPTIMIZED:
                body_hits[filename].add(lineno)
        return sys.monitoring.DISABLE

    sys.monitoring.use_tool_id(Recorder.TOOL, 'sqlglot initial imports')
    sys.monitoring.register_callback(Recorder.TOOL, sys.monitoring.events.LINE, line)
    sys.monitoring.set_events(Recorder.TOOL, sys.monitoring.events.LINE)
    pytest.main(['--collect-only', '-q', '-p', 'no:cacheprovider', 'tests'])
    for path in sorted((root / 'sqlglot').rglob('*.py')):
        name = path.relative_to(root).with_suffix('').as_posix().replace('/', '.')
        if '.__main__' in name:
            continue
        try:
            importlib.import_module(name.removesuffix('.__init__'))
        except ImportError:
            pass  # Optional extensions do not ship in the scorer environment.
    sys.monitoring.set_events(Recorder.TOOL, 0)
    sys.monitoring.free_tool_id(Recorder.TOOL)
    names = set()
    for code in used_codes.values():
        lines = hits[code.co_filename[len(prefix):]]
        names.update(name for name, first, last in read_names(code) if first in lines or last in lines)
    with output.open('wb') as f:
        pickle.dump({'startup': dict(hits), 'startup_body': dict(body_hits), 'startup_names': names}, f)


def recover_contexts(result, extra):
    """Union independent complete traces by node ID. A fresh run can remove
    allocation-counter interference caused by profiling thousands of prior tests.
    """
    numbers = {name: i for i, name in enumerate(result['ids'])}
    for i, name in enumerate(extra['ids']):
        source_bit = 1 << i
        if extra['incomplete'] & source_bit:
            continue
        dest_bit = 1 << numbers[name]
        for group in ('reads', 'classes', 'names', 'modules'):
            dest = result[group]
            for key, mask in extra[group].items():
                if mask & source_bit:
                    dest[key] = dest.get(key, 0) | dest_bit
        for filename, hits in extra['lines'].items():
            lines = result['lines'].setdefault(filename, {})
            for line, mask in hits.items():
                if mask & source_bit:
                    lines[line] = lines.get(line, 0) | dest_bit
        result['incomplete'] &= ~dest_bit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--startup-only', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--single-attempt', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('tests', nargs='*', help='Optional pytest paths for development')
    args = parser.parse_args()
    root = args.source.resolve()
    output = args.output.resolve()
    os.chdir(root)
    os.environ['SKIP_INTEGRATION'] = '0'
    sys.path.insert(0, str(root))
    if args.startup_only:
        startup_map(root, output)
        return
    startup = {}
    if not args.single_attempt:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'startup.pkl'
            subprocess.run([sys.executable, __file__, '--startup-only', str(root), str(path)],
                           check=True, stdout=subprocess.DEVNULL)
            with path.open('rb') as f:
                startup = pickle.load(f)
    futures.ProcessPoolExecutor = InlinePool
    recorder = Recorder(root)
    recorder.install_receivers()
    test_args = ['-q', '--tb=short', '--disable-warnings', '-p', 'no:cacheprovider']
    # Continue mapping runnable files when an optional dependency fails collection.
    code = pytest.main([*test_args, '--continue-on-collection-errors', *(args.tests or ['tests'])],
                       plugins=[recorder])
    if not recorder.ids or code not in (0, 1):
        raise SystemExit(f'Could not map test suite (pytest status {code})')
    result = dict(recorder.result(), **startup)
    if recorder.failures and not args.single_attempt:
        # Only trust recovery when every subtest of the retraced case passes.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'retry.pkl.gz'
            subprocess.run([sys.executable, __file__, '--single-attempt', str(root), str(path),
                            *sorted(recorder.failures)], check=True)
            with gzip.open(path, 'rb') as f:
                recover_contexts(result, pickle.load(f))
    with gzip.GzipFile(filename=str(output), mode='wb', compresslevel=6, mtime=0) as out:
        pickle.dump(result, out, protocol=5)
    print(f'Wrote {len(recorder.ids)} test contexts to {output}', file=sys.stderr)


if __name__ == '__main__':
    main()
