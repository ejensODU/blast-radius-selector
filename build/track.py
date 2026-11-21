"""Build-time read tracking for module-level and class-level dicts of the pinned sqlglot.

Every such dict is replaced, right after its module finishes importing, by a
subclass that records which keys are read. Reads made while a sqlglot module is
being imported are not recorded: whatever import-time code derives from a dict
is visible to the import-state diff instead. The original object is emptied so a
reference taken before the replacement fails loudly instead of reading unseen.
"""
from __future__ import annotations

import enum
import importlib.abc
import importlib.machinery
import sys

READS = set()      # (cell index, raw key)
BULK = set()       # cell index: every key was read, or the dict was mutated
CELLS = []         # cell index -> (path, qualname, attribute)
ALIASES = []       # (cell, cell it shares an object with)
STALE = []         # cells whose wrapping failed
_depth = 0         # > 0 while a sqlglot module body is executing
_quiet = 0
ROOT = ""


class TrackedDict(dict):
    __slots__ = ("_cell",)

    def _one(self, key):
        if not _depth and not _quiet:
            try:
                READS.add((self._cell, key))
            except (AttributeError, TypeError):
                pass

    def _all(self):
        if not _depth and not _quiet:
            try:
                BULK.add(self._cell)
            except AttributeError:
                pass

    def __getitem__(self, key):
        self._one(key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._one(key)
        return dict.get(self, key, default)

    def __contains__(self, key):
        self._one(key)
        return dict.__contains__(self, key)

    def __iter__(self):
        self._all()
        return dict.__iter__(self)

    def keys(self):
        self._all()
        return dict.keys(self)

    def values(self):
        self._all()
        return dict.values(self)

    def items(self):
        self._all()
        return dict.items(self)

    def __len__(self):
        self._all()
        return dict.__len__(self)

    def __eq__(self, other):
        self._all()
        return dict.__eq__(self, other)

    def __ne__(self, other):
        self._all()
        return dict.__ne__(self, other)

    __hash__ = None

    def __repr__(self):
        self._all()
        return dict.__repr__(self)

    def copy(self):
        self._all()
        return dict(dict.items(self))

    def __or__(self, other):
        self._all()
        return dict.__or__(dict(dict.items(self)), other)

    def __ror__(self, other):
        self._all()
        return dict.__or__(dict(other), dict(dict.items(self)))

    def __reduce_ex__(self, protocol):
        self._all()
        return (dict, (dict(dict.items(self)),))

    def _write(self):
        self._all()

    def __setitem__(self, key, value):
        self._write()
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        self._write()
        dict.__delitem__(self, key)

    def setdefault(self, key, default=None):
        self._write()
        return dict.setdefault(self, key, default)

    def pop(self, *args):
        self._write()
        return dict.pop(self, *args)

    def popitem(self):
        self._write()
        return dict.popitem(self)

    def update(self, *args, **kwargs):
        self._write()
        dict.update(self, *args, **kwargs)

    def clear(self):
        self._write()
        dict.clear(self)

    def __ior__(self, other):
        self._write()
        dict.update(self, other)
        return self


def _wrap(value, cell, done):
    known = done.get(id(value))
    if known is not None:
        ALIASES.append((cell, known._cell))
        return known
    tracked = TrackedDict()
    dict.update(tracked, value)
    tracked._cell = len(CELLS)
    CELLS.append(cell)
    done[id(value)] = tracked
    return tracked


def _classes(module):
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


def wrap_module(module):
    filename = getattr(module, "__file__", None) or ""
    if not filename.startswith(ROOT):
        return
    path = filename[len(ROOT):]
    done, originals = {}, []
    for name, value in list(vars(module).items()):
        if type(value) is dict and not name.startswith("__"):
            vars(module)[name] = _wrap(value, (path, "", name), done)
            originals.append(value)
    for cls in _classes(module):
        if issubclass(cls, enum.Enum):
            continue
        for name, value in list(vars(cls).items()):
            if type(value) is dict and not name.startswith("__"):
                try:
                    type.__setattr__(cls, name, _wrap(value, (path, cls.__qualname__, name), done))
                    originals.append(value)
                except (AttributeError, TypeError):
                    STALE.append((path, cls.__qualname__, name))
    for value in originals:
        value.clear()


class _Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        global _depth
        _depth += 1
        try:
            super().exec_module(module)
        finally:
            _depth -= 1
        wrap_module(module)
        hook = HOOKS.get(module.__name__)
        if hook:
            hook(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.origin and spec.origin.endswith(".py") and spec.origin.startswith(ROOT):
            spec.loader = _Loader(fullname, spec.origin)
        return spec


class LineageDict(TrackedDict):
    """A dict derived from tracked dicts at test time; a read is charged to the source entry."""
    __slots__ = ("_source",)

    def _one(self, key):
        if _depth or _quiet:
            return
        try:
            source = self._source.get(key)
        except TypeError:
            source = None
        if source is not None:
            READS.add((source, key))
        else:
            READS.add((self._cell, key))


def _hook_generator(module):
    """The dispatch table is built once per generator class from TRANSFORMS and the *_sql methods."""
    original = module._build_dispatch

    def build(cls):
        global _quiet
        _quiet += 1
        try:
            plain = original(cls)
            transforms = cls.TRANSFORMS
            owner = getattr(transforms, "_cell", None)
            present = set(dict.keys(transforms)) if isinstance(transforms, dict) else set()
        finally:
            _quiet -= 1
        out = LineageDict()
        dict.update(out, plain)
        out._cell = len(CELLS)
        CELLS.append(("<derived>", f"{cls.__module__}.{cls.__qualname__}", "dispatch"))
        out._source = {key: owner for key in present} if owner is not None else {}
        return out

    module._build_dispatch = build


HOOKS = {"sqlglot.generator": _hook_generator}


def install(root):
    global ROOT
    ROOT = root.rstrip("/") + "/"
    sys.meta_path.insert(0, _Finder())
