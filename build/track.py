"""Build-time read tracking for dicts, sets, frozensets, and scalar class attributes of pinned sqlglot.

Every module-level and class-level dict/set/frozenset is replaced right after its module
finishes importing by a subclass that records per-key / per-element reads (and bulk reads).
Every immutable scalar class attribute (bool/int/float/str/bytes/None/Enum/scalar-tuple)
on non-Enum, non-dataclass, non-NamedTuple classes is replaced by a non-data descriptor
that records per-cell attribute reads. Reads made while a sqlglot module is being imported
are not recorded: import-time derived state is visible to the import-state diff instead.
"""
from __future__ import annotations

import dataclasses
import enum
import importlib.abc
import importlib.machinery
import sys

READS = set()       # (cell index, raw key/element)
BULK = set()        # cell index: every key/element was read, or container was mutated
CELLS = []          # cell index -> (path, qualname, attribute)
ALIASES = []        # (cell, cell it shares an object with)
STALE = []          # cells whose wrapping failed
ATTR_CELLS = []     # attr cell index -> (path, qualname, attribute)
ATTR_READS = set()  # attr cell index read during test
_depth = 0          # > 0 while a sqlglot module body is executing
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

    def __bool__(self):
        self._all()
        return bool(dict.__len__(self))

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


class TrackedSet(set):
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

    def __contains__(self, key):
        self._one(key)
        return set.__contains__(self, key)

    def __iter__(self):
        self._all()
        return set.__iter__(self)

    def __len__(self):
        self._all()
        return set.__len__(self)

    def __bool__(self):
        self._all()
        return bool(set.__len__(self))

    def __eq__(self, other):
        self._all()
        return set.__eq__(self, other)

    def __ne__(self, other):
        self._all()
        return set.__ne__(self, other)

    def __lt__(self, other):
        self._all()
        return set.__lt__(self, other)

    def __le__(self, other):
        self._all()
        return set.__le__(self, other)

    def __gt__(self, other):
        self._all()
        return set.__gt__(self, other)

    def __ge__(self, other):
        self._all()
        return set.__ge__(self, other)

    __hash__ = None

    def __repr__(self):
        self._all()
        return set.__repr__(self)

    def copy(self):
        self._all()
        return set(set.__iter__(self))

    def __or__(self, other):
        self._all()
        return set(set.__iter__(self)) | other

    def __ror__(self, other):
        self._all()
        return other | set(set.__iter__(self))

    def __and__(self, other):
        self._all()
        return set(set.__iter__(self)) & other

    def __rand__(self, other):
        self._all()
        return other & set(set.__iter__(self))

    def __sub__(self, other):
        self._all()
        return set(set.__iter__(self)) - other

    def __rsub__(self, other):
        self._all()
        return other - set(set.__iter__(self))

    def __xor__(self, other):
        self._all()
        return set(set.__iter__(self)) ^ other

    def __rxor__(self, other):
        self._all()
        return other ^ set(set.__iter__(self))

    def union(self, *others):
        self._all()
        return set(set.__iter__(self)).union(*others)

    def intersection(self, *others):
        self._all()
        return set(set.__iter__(self)).intersection(*others)

    def difference(self, *others):
        self._all()
        return set(set.__iter__(self)).difference(*others)

    def symmetric_difference(self, other):
        self._all()
        return set(set.__iter__(self)).symmetric_difference(other)

    def issubset(self, other):
        self._all()
        return set.issubset(self, other)

    def issuperset(self, other):
        self._all()
        return set.issuperset(self, other)

    def isdisjoint(self, other):
        self._all()
        return set.isdisjoint(self, other)

    def __reduce_ex__(self, protocol):
        self._all()
        return (set, (list(set.__iter__(self)),))

    def add(self, elem):
        self._all()
        set.add(self, elem)

    def remove(self, elem):
        self._all()
        set.remove(self, elem)

    def discard(self, elem):
        self._all()
        set.discard(self, elem)

    def pop(self):
        self._all()
        return set.pop(self)

    def clear(self):
        self._all()
        set.clear(self)

    def update(self, *others):
        self._all()
        set.update(self, *others)

    def intersection_update(self, *others):
        self._all()
        set.intersection_update(self, *others)

    def difference_update(self, *others):
        self._all()
        set.difference_update(self, *others)

    def symmetric_difference_update(self, other):
        self._all()
        set.symmetric_difference_update(self, other)

    def __ior__(self, other):
        self._all()
        set.update(self, other)
        return self

    def __iand__(self, other):
        self._all()
        set.intersection_update(self, other)
        return self

    def __isub__(self, other):
        self._all()
        set.difference_update(self, other)
        return self

    def __ixor__(self, other):
        self._all()
        set.symmetric_difference_update(self, other)
        return self


class TrackedFrozenset(frozenset):
    __slots__ = ("_cell",)

    def __new__(cls, iterable=()):
        return super().__new__(cls, iterable)

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

    def __contains__(self, key):
        self._one(key)
        return frozenset.__contains__(self, key)

    def __iter__(self):
        self._all()
        return frozenset.__iter__(self)

    def __len__(self):
        self._all()
        return frozenset.__len__(self)

    def __bool__(self):
        self._all()
        return bool(frozenset.__len__(self))

    def __eq__(self, other):
        self._all()
        return frozenset.__eq__(self, other)

    def __ne__(self, other):
        self._all()
        return frozenset.__ne__(self, other)

    def __lt__(self, other):
        self._all()
        return frozenset.__lt__(self, other)

    def __le__(self, other):
        self._all()
        return frozenset.__le__(self, other)

    def __gt__(self, other):
        self._all()
        return frozenset.__gt__(self, other)

    def __ge__(self, other):
        self._all()
        return frozenset.__ge__(self, other)

    def __hash__(self):
        self._all()
        return frozenset.__hash__(self)

    def __repr__(self):
        self._all()
        return frozenset.__repr__(self)

    def copy(self):
        self._all()
        return frozenset(frozenset.__iter__(self))

    def __or__(self, other):
        self._all()
        return frozenset(frozenset.__iter__(self)) | other

    def __ror__(self, other):
        self._all()
        return other | frozenset(frozenset.__iter__(self))

    def __and__(self, other):
        self._all()
        return frozenset(frozenset.__iter__(self)) & other

    def __rand__(self, other):
        self._all()
        return other & frozenset(frozenset.__iter__(self))

    def __sub__(self, other):
        self._all()
        return frozenset(frozenset.__iter__(self)) - other

    def __rsub__(self, other):
        self._all()
        return other - frozenset(frozenset.__iter__(self))

    def __xor__(self, other):
        self._all()
        return frozenset(frozenset.__iter__(self)) ^ other

    def __rxor__(self, other):
        self._all()
        return other ^ frozenset(frozenset.__iter__(self))

    def union(self, *others):
        self._all()
        return frozenset(frozenset.__iter__(self)).union(*others)

    def intersection(self, *others):
        self._all()
        return frozenset(frozenset.__iter__(self)).intersection(*others)

    def difference(self, *others):
        self._all()
        return frozenset(frozenset.__iter__(self)).difference(*others)

    def symmetric_difference(self, other):
        self._all()
        return frozenset(frozenset.__iter__(self)).symmetric_difference(other)

    def issubset(self, other):
        self._all()
        return frozenset.issubset(self, other)

    def issuperset(self, other):
        self._all()
        return frozenset.issuperset(self, other)

    def isdisjoint(self, other):
        self._all()
        return frozenset.isdisjoint(self, other)

    def __reduce_ex__(self, protocol):
        self._all()
        return (frozenset, (list(frozenset.__iter__(self)),))


class _TrackedScalar:
    """Non-data descriptor that records reads of a scalar class attribute."""
    __slots__ = ("_val", "_cell")

    def __init__(self, val, cell):
        self._val = val
        self._cell = cell

    def __get__(self, obj, objtype=None):
        if not _depth and not _quiet:
            ATTR_READS.add(self._cell)
        return self._val


_SCALAR_TYPES = (bool, int, float, str, bytes, type(None))


def _is_scalar(v, depth=0):
    t = type(v)
    if t in _SCALAR_TYPES or isinstance(v, enum.Enum):
        return True
    if t is tuple and depth < 2 and len(v) <= 64:
        return all(_is_scalar(x, depth + 1) for x in v)
    return False


def _wrap_container(value, cell, done):
    known = done.get(id(value))
    if known is not None:
        ALIASES.append((cell, known._cell))
        return known
    t = type(value)
    if t is dict:
        tracked = TrackedDict()
        dict.update(tracked, value)
    elif t is set:
        tracked = TrackedSet()
        set.update(tracked, value)
    else:
        tracked = TrackedFrozenset(value)
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
        if type(value) in (dict, set, frozenset) and not name.startswith("__"):
            vars(module)[name] = _wrap_container(value, (path, "", name), done)
            if type(value) in (dict, set):
                originals.append(value)
    skip_scalars_mod = (module.__name__ == "sqlglot._typing")
    for cls in _classes(module):
        if issubclass(cls, enum.Enum):
            continue
        can_scalar = not skip_scalars_mod and not dataclasses.is_dataclass(cls) and not issubclass(cls, tuple)
        for name, value in list(vars(cls).items()):
            if name.startswith("__"):
                continue
            tv = type(value)
            if tv in (dict, set, frozenset):
                try:
                    type.__setattr__(cls, name, _wrap_container(value, (path, cls.__qualname__, name), done))
                    if tv in (dict, set):
                        originals.append(value)
                except (AttributeError, TypeError):
                    STALE.append((path, cls.__qualname__, name))
            elif can_scalar and _is_scalar(value):
                try:
                    idx = len(ATTR_CELLS)
                    ATTR_CELLS.append((path, cls.__qualname__, name))
                    type.__setattr__(cls, name, _TrackedScalar(value, idx))
                except (AttributeError, TypeError):
                    ATTR_CELLS.pop()
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
