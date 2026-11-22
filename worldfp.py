"""Fingerprint the import-time state of sqlglot: every module global and class attribute.

Two worlds (pristine and patched) are compared cell by cell, so an edit to a
module-level or class-level statement is reduced to the exact values it changed,
including values derived from it while modules were being imported.
"""
from __future__ import annotations

import enum
import functools
import hashlib
import importlib
import re
import sys
import types

SKIP_GLOBALS = {"__builtins__", "__cached__", "__file__", "__loader__", "__spec__", "__path__", "__warningregistry__"}
SKIP_CLASS = {"__dict__", "__weakref__", "__module__", "__firstlineno__", "__static_attributes__", "__qualname__"}


def _h(*parts):
    """Combine parts into one fingerprint.

    The interpreter's own tuple hash, which is the same in every process started
    with PYTHONHASHSEED=0 on one Python build. Both snapshots are taken that way.
    """
    try:
        return hash(parts)
    except TypeError:
        return hash(tuple(p if isinstance(p, (bytes, str, int)) else repr(p) for p in parts))


SEED_CHECK = ("fingerprint", "seed", 0, b"check")


def keytok(k) -> str:
    """A stable name for a container key or set element, shared with the read tracker."""
    t = type(k)
    if t is str:
        return "s:" + k
    if t is int or t is bool:
        return "i:" + repr(k)
    if isinstance(k, enum.Enum):
        return f"e:{type(k).__qualname__}.{k.name}"
    if isinstance(k, type):
        return f"c:{k.__module__}.{k.__qualname__}"
    if t is tuple:
        return "t:(" + ",".join(keytok(x) for x in k) + ")"
    if k is None:
        return "n:"
    if t is float or t is bytes:
        return "v:" + repr(k)
    return "o:" + t.__qualname__


class Fingerprinter:
    def __init__(self):
        self.memo = {}
        self.active = set()

    def code(self, c: types.CodeType):
        consts = [self.code(x) if isinstance(x, types.CodeType) else self.fp(x) for x in c.co_consts]
        return _h("code", c.co_name, c.co_argcount, c.co_posonlyargcount, c.co_kwonlyargcount, c.co_flags,
                  c.co_code, c.co_names, c.co_varnames, c.co_freevars, c.co_cellvars, c.co_exceptiontable, *consts)

    def fp(self, o):
        t = type(o)
        if o is None or t in (bool, int, float, complex, str, bytes):
            return _h("c", t.__name__, repr(o))
        i = id(o)
        got = self.memo.get(i)
        if got is not None:
            return got
        if i in self.active:
            return _h("cycle", t.__qualname__)
        self.active.add(i)
        try:
            r = self._fp(o, t)
        except Exception as error:  # an unreadable object is opaque, never fatal
            r = _h("error", t.__qualname__, type(error).__name__)
        self.active.discard(i)
        self.memo[i] = r
        return r

    def _fp(self, o, t):
        if t in (tuple, list):
            return _h(t.__name__, *[self.fp(x) for x in o])
        if isinstance(o, dict):
            return _h("map", t.__qualname__, *sorted(_h(self.fp(k), self.fp(v)) for k, v in dict.items(o)))
        if isinstance(o, (set, frozenset)):
            return _h("set", *sorted(self.fp(x) for x in (set.__iter__(o) if isinstance(o, set) else o)))
        if isinstance(o, enum.Enum):
            return _h("enum", t.__module__, t.__qualname__, o.name)
        if isinstance(o, type):
            return _h("cls", o.__module__, o.__qualname__)
        if t is types.ModuleType:
            return _h("mod", o.__name__)
        if t is types.FunctionType:
            cells = []
            for cell in o.__closure__ or ():
                try:
                    cells.append(self.fp(cell.cell_contents))
                except ValueError:
                    cells.append("empty")
            return _h("fn", o.__module__, o.__qualname__, self.code(o.__code__), self.fp(o.__defaults__),
                      self.fp(o.__kwdefaults__), *cells)
        if t is types.CodeType:
            return self.code(o)
        if t in (staticmethod, classmethod):
            return _h(t.__name__, self.fp(o.__func__))
        if t is property:
            return _h("property", self.fp(o.fget), self.fp(o.fset), self.fp(o.fdel))
        if t is functools.partial:
            return _h("partial", self.fp(o.func), self.fp(o.args), self.fp(o.keywords))
        if t is types.MethodType:
            return _h("method", self.fp(o.__func__), self.fp(type(o.__self__)))
        if t in (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
                 types.MethodWrapperType, types.ClassMethodDescriptorType, types.GetSetDescriptorType,
                 types.MemberDescriptorType):
            return _h("builtin", getattr(o, "__qualname__", repr(o)))
        if t is re.Pattern:
            return _h("re", o.pattern, o.flags)
        if t is functools._lru_cache_wrapper:
            return _h("lru", self.fp(o.__wrapped__))
        parts = []
        d = getattr(o, "__dict__", None)
        if isinstance(d, dict):
            parts += [_h(k, self.fp(v)) for k, v in d.items()]
        for klass in t.__mro__:
            for name in getattr(klass, "__slots__", ()) if isinstance(getattr(klass, "__slots__", ()), (tuple, list)) else ():
                if name in ("__dict__", "__weakref__"):
                    continue
                try:
                    parts.append(_h(name, self.fp(object.__getattribute__(o, name))))
                except AttributeError:
                    pass
        if parts:
            return _h("obj", t.__module__, t.__qualname__, *sorted(parts))
        text = repr(o)
        return _h("opaque", t.__module__, t.__qualname__, "" if " at 0x" in text else text)


def _classes(module, prefix):
    """Classes defined in this module, including nested ones."""
    seen, stack, out = set(), [], []
    for value in list(vars(module).values()):
        if isinstance(value, type) and value.__module__ == module.__name__:
            stack.append(value)
    while stack:
        cls = stack.pop()
        if id(cls) in seen:
            continue
        seen.add(id(cls))
        out.append(cls)
        for value in list(vars(cls).values()):
            if isinstance(value, type) and value.__module__ == module.__name__ and value.__qualname__.startswith(cls.__qualname__ + "."):
                stack.append(value)
    return out


def snapshot(root: str, names=None):
    """cell -> fingerprint, plus per-key fingerprints for dict and set cells.

    A cell is (path, qualname, attribute); qualname is '' for a module global.
    """
    fper = Fingerprinter()
    cells, parts = {}, {}
    prefix = root.rstrip("/") + "/"

    def record(cell, value):
        cells[cell] = fper.fp(value)
        if isinstance(value, dict):
            parts[cell] = {keytok(k): _h(fper.fp(k), fper.fp(v)) for k, v in dict.items(value)}
        elif isinstance(value, (set, frozenset)):
            parts[cell] = {keytok(x): fper.fp(x) for x in list(value)}

    for name, module in sorted(sys.modules.items()):
        filename = getattr(module, "__file__", None) or ""
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or not filename.startswith(prefix):
            continue
        if names is not None and name not in names:
            continue
        path = filename[len(prefix):]
        for attr, value in list(vars(module).items()):
            if attr in SKIP_GLOBALS:
                continue
            if isinstance(value, types.ModuleType):
                record((path, "", attr), value)
                continue
            record((path, "", attr), value)
        for cls in _classes(module, prefix):
            q = cls.__qualname__
            cells[(path, q, "<bases>")] = _h(*[f"{b.__module__}.{b.__qualname__}" for b in cls.__mro__])
            for attr, value in list(vars(cls).items()):
                if attr in SKIP_CLASS:
                    continue
                record((path, q, attr), value)
    return cells, parts


def module_names(root: str):
    from pathlib import Path
    out = []
    for path in sorted(Path(root, "sqlglot").rglob("*.py")):
        name = path.relative_to(root).with_suffix("").as_posix().replace("/", ".")
        if ".__main__" in name or name.endswith("__main__"):
            continue
        out.append(name.removesuffix(".__init__"))
    return out


def load(names):
    """Import the modules; return {name: error text} for the ones that failed."""
    failed = {}
    for name in names:
        try:
            importlib.import_module(name)
        except BaseException as error:  # SyntaxError, ImportError, anything at import time
            failed[name] = f"{type(error).__name__}: {error}"[:300]
    return failed


if __name__ == "__main__":
    import gzip, os, pickle, time
    args = sys.argv[1:]
    listed = None
    if args and args[0] == "--names":
        listed = [line for line in sys.stdin.read().split("\n") if line]
        args = args[1:]
    root = os.path.abspath(args[0])
    sys.argv = [sys.argv[0], *args]
    sys.path.insert(0, root)
    t0 = time.process_time()
    failed = load(listed if listed is not None else module_names(root))
    t1 = time.process_time()
    cells, parts = snapshot(root, set(listed) if listed is not None else None)
    t2 = time.process_time()
    if len(sys.argv) > 2:
        opener = (lambda p: gzip.GzipFile(p, "wb", compresslevel=6, mtime=0)) if sys.argv[2].endswith(".gz") else (lambda p: open(p, "wb"))
        with opener(sys.argv[2]) as out:
            pickle.dump({"cells": cells, "parts": parts, "failed": failed, "seed": _h(*SEED_CHECK)}, out, protocol=5)
    print(f"modules failed={len(failed)} cells={len(cells)} keyed={len(parts)} keys={sum(len(v) for v in parts.values())} "
          f"import_cpu={t1-t0:.2f}s snapshot_cpu={t2-t1:.2f}s", file=sys.stderr)
    for name, error in list(failed.items())[:5]:
        print("  failed", name, error, file=sys.stderr)
