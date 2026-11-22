"""Edits that are not a plain change inside an existing library or fixture file.

Added, deleted, renamed and binary files, files no test reads, and changes to
the shape of a test module (renamed or removed tests, helpers, class bodies) are
resolved here against the same measured dependencies as everything else. Each
file section of a patch gets one of three answers: a set of tests, "use the
ordinary analysis", or "unknown" (the whole suite).
"""
from __future__ import annotations

import ast
import configparser
import importlib.util
import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

APP = Path(__file__).resolve().parent
FULL = None  # the answer "run everything"

# Files pytest or the interpreter picks up by name alone.
_HOOK_NAMES = {"conftest.py", "pytest.ini", "tox.ini", "sitecustomize.py", "usercustomize.py", ".pytest.ini"}
_CONFIG_NAMES = {"setup.cfg", "pyproject.toml"}
_IMPORT_SUFFIXES = (".py", ".pyc", ".so", ".pyd")


@dataclass
class Section:
    old: str
    new: str
    status: str  # modify | add | delete | rename | mode
    binary: bool = False
    hunks: bool = False
    lines: list = field(default_factory=list)

    @property
    def text(self):
        return "".join(self.lines)


def _unquote(name):
    name = name.rstrip("\r\n").split("\t", 1)[0]
    if name.startswith('"'):
        try:
            words = shlex.split(name)
        except ValueError:
            return None
        name = words[0] if len(words) == 1 else None
    return name


def _strip(name):
    if name is None:
        return None
    return name[2:] if name[:2] in ("a/", "b/") else name


def sections(text):
    """Split a git patch into per-file sections. None when it is not in git form."""
    result = []
    current = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            current = Section("", "", "modify")
            current.lines.append(line)
            result.append(current)
            try:
                names = shlex.split(line.rstrip("\r\n"))
            except ValueError:
                names = []
            if len(names) == 4:
                current.old, current.new = _strip(names[2]) or "", _strip(names[3]) or ""
            else:
                # Unquoted names with spaces: "a/X b/X" for an unrenamed file.
                rest = line[len("diff --git "):].rstrip("\r\n")
                half = (len(rest) - 1) // 2
                if rest[:half] and rest[half] == " " and rest[:half][2:] == rest[half + 1:][2:]:
                    current.old = current.new = rest[:half][2:]
            continue
        if current is None:
            if line.strip():
                return None
            continue
        current.lines.append(line)
        if current.hunks:
            continue
        if line.startswith("new file mode"):
            current.status = "add"
        elif line.startswith("deleted file mode"):
            current.status = "delete"
        elif line.startswith("rename from "):
            current.status, current.old = "rename", _unquote(line[len("rename from "):]) or ""
        elif line.startswith("rename to "):
            current.status, current.new = "rename", _unquote(line[len("rename to "):]) or ""
        elif line.startswith("copy from "):
            current.status = "add"
        elif line.startswith("copy to "):
            current.new = _unquote(line[len("copy to "):]) or ""
        elif line.startswith(("old mode", "new mode")) and current.status == "modify":
            current.status = "mode"
        elif line.startswith(("GIT binary patch", "Binary files ")):
            current.binary = True
        elif line.startswith("--- "):
            name = _unquote(line[4:])
            if name == "/dev/null":
                current.status = "add"
            elif name is not None and current.status != "rename":
                current.old = _strip(name)
        elif line.startswith("+++ "):
            name = _unquote(line[4:])
            if name == "/dev/null":
                current.status = "delete"
            elif name is not None and current.status != "rename":
                current.new = _strip(name)
        elif line.startswith("@@"):
            current.hunks = True
            if current.status == "mode":
                current.status = "modify"
    return result or None


def _bad(path):
    parts = PurePosixPath(path).parts
    return not path or PurePosixPath(path).is_absolute() or ".." in parts


def _inert(node, taken):
    """A new module-level statement that only binds fresh names to literals or standard-library modules."""
    if isinstance(node, ast.Import):
        return all((alias.asname or alias.name).split(".")[0] not in taken
                   and alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return (node.level == 0 and (node.module or "").split(".")[0] in sys.stdlib_module_names
                and all(alias.name != "*" and (alias.asname or alias.name) not in taken for alias in node.names))
    if isinstance(node, ast.Assign):
        try:
            ast.literal_eval(node.value)
        except Exception:
            return False
        return all(isinstance(target, ast.Name) and target.id not in taken
                   and not target.id.startswith("__") and target.id != "pytestmark" for target in node.targets)
    return False


class Resolver:
    def __init__(self, index, root):
        self.index = index
        self.root = Path(root)
        data = index.profile
        self.lines = data["lines"]
        self.reads = data["reads"]
        try:
            self.fs = json.loads((APP / "fsdeps.json").read_text())
        except (OSError, ValueError):
            self.fs = None

    # ---- measured users -------------------------------------------------
    def file_tests(self, test_files):
        mask = 0
        for name in test_files:
            mask |= self.index.by_file.get(name, 0)
        return mask

    def readers(self, path):
        """Tests recorded opening or checking this path (or anything below it)."""
        mask = self.reads.get(path, 0)
        prefix = path.rstrip("/") + "/"
        for name, value in self.reads.items():
            if name.startswith(prefix):
                mask |= value
        if self.fs is not None:
            # Opens seen at system-call level, per test file: covers native code and
            # child processes. Only files with no test-level record add anything.
            table = self.fs.get("opened", {})
            for name, users in table.items():
                if name == path or name.startswith(prefix):
                    for test_file in users:
                        tests = self.index.by_file.get(test_file, 0)
                        if not tests & mask:
                            mask |= tests
        return mask

    def users(self, path):
        """Every test that declares, executes or reads any part of a Python file."""
        mask = self.index.by_file.get(path, 0) | self.readers(path)
        for value in self.lines.get(path, {}).values():
            mask |= value
        if self.fs is not None:
            # Test files whose processes open (import) this module.
            mask |= self.file_tests(self.fs.get("opened", {}).get(path, ()))
        return mask

    def covering(self, path, first, last):
        mask = 0
        table = self.lines.get(path, {})
        for line in range(first, last + 1):
            mask |= table.get(line, 0)
        return mask

    def known(self, path):
        return (path in self.lines or path in self.index.by_file
                or (self.fs is not None and path in self.fs.get("opened", {})))

    # ---- file-level answers ----------------------------------------------
    def appears(self, path):
        """A path that did not exist before now does."""
        name = PurePosixPath(path).name
        parts = PurePosixPath(path).parts
        if name in _HOOK_NAMES or name in _CONFIG_NAMES or name.endswith(".pth"):
            return FULL
        if self.fs is None:
            return FULL
        mask = self.file_tests(self.fs.get("probed_missing", {}).get(path, ()))
        for depth in range(len(parts)):
            folder = "/".join(parts[:depth]) or "."
            mask |= self.file_tests(self.fs.get("listed", {}).get(folder, ()))
        if name.endswith(_IMPORT_SUFFIXES) or name == "__init__.py":
            if name == "__init__.py" or name.startswith("__init__."):
                return FULL  # turns a folder into a package
            stem = name.split(".", 1)[0]
            dotted = ".".join((*parts[:-1], stem))
            failed = set(self.fs.get("failed_imports", ()))
            if dotted in failed or stem in failed and len(parts) == 1:
                return FULL
            if len(parts) == 1:
                # The repository root is on sys.path ahead of the standard library.
                if stem in sys.stdlib_module_names:
                    return FULL
                try:
                    if importlib.util.find_spec(stem) is not None:
                        return FULL
                except (ImportError, ValueError, AttributeError):
                    return FULL
            elif not (self.root / "/".join(parts[:-1]) / "__init__.py").is_file():
                # A script folder: importable only through sys.path tricks we did not record.
                if parts[0] in ("sqlglot", "tests"):
                    return FULL
        return mask

    def vanishes(self, path):
        name = PurePosixPath(path).name
        if name in _HOOK_NAMES or name in _CONFIG_NAMES or name == "__init__.py":
            return FULL
        if path.endswith(".py"):
            if path.startswith("sqlglot/"):
                return FULL
            return self.users(path)
        return self.readers(path)

    def config(self, path, text):
        """An edited setup.cfg or pyproject.toml matters to pytest only through its own tables."""
        try:
            old = (self.root / path).read_text(encoding="utf-8")
            from impact import apply_file, parse_patch
            parts = parse_patch(text)
            if len(parts) != 1 or not parts[0].valid:
                return FULL
            new, _ = apply_file(old, parts[0])
            if path.endswith(".toml"):
                import tomllib
                before, after = tomllib.loads(old), tomllib.loads(new)
                same = before.get("tool", {}).get("pytest") == after.get("tool", {}).get("pytest")
            else:
                before, after = configparser.ConfigParser(), configparser.ConfigParser()
                before.read_string(old)
                after.read_string(new)
                pick = lambda cfg: {s: dict(cfg[s]) for s in cfg.sections() if "pytest" in s}
                same = pick(before) == pick(after)
        except Exception:
            return FULL
        # pytest itself opens these in every run; beyond that only recorded readers matter.
        return self.reads.get(path, 0) if same else FULL

    # ---- test modules ----------------------------------------------------
    def test_module(self, path, text):
        """A test or test-support module whose shape changed: compare it unit by unit."""
        try:
            old = (self.root / path).read_text(encoding="utf-8")
            from impact import apply_file, parse_patch
            parts = parse_patch(text)
            if len(parts) != 1 or not parts[0].valid:
                return FULL
            new, _ = apply_file(old, parts[0])
            before = ast.parse(old)
        except Exception:
            return FULL
        everyone = self.users(path)
        try:
            after = ast.parse(new)
        except (SyntaxError, ValueError, RecursionError):
            return everyone  # the module no longer imports
        mask = self.reads.get(path, 0)
        startup = self.index.profile["startup_body"].get(path, ())
        for tree in (before, after):
            # Nothing in the suite reads a docstring of test code.
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    first = node.body[0]
                    if (len(node.body) > 1 and isinstance(first, ast.Expr)
                            and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)):
                        del node.body[0]

        def split(body):
            units, rest = {}, []
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name not in units:
                    units[node.name] = node
                else:
                    rest.append(ast.dump(node))
            return units, rest

        def bound(body):
            names = set()
            for node in body:
                for sub in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [node]:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.add(sub.name)
                    elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                        names.add(sub.id)
                    elif isinstance(sub, ast.alias):
                        names.add((sub.asname or sub.name).split(".")[0])
            return names

        def body_span(node):
            return node.body[0].lineno, node.end_lineno

        def own(classname, method):
            mask = 0
            for key, row in self.index.row.items():
                if row["file"] == path and key == f"{path}::{classname}::{method}":
                    mask |= 1 << self.index.names.index(key)
            return mask

        def class_tests(classname):
            mask = 0
            prefix = f"{path}::{classname}::"
            for position, key in enumerate(self.index.names):
                if key.startswith(prefix) and key in self.index.row:
                    mask |= 1 << position
            return mask

        old_units, old_rest = split(before.body)
        new_units, new_rest = split(after.body)
        old_names = bound(before.body)
        if old_rest != new_rest:
            remaining = list(old_rest)
            added = []
            for node in after.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                text_form = ast.dump(node)
                if text_form in remaining:
                    remaining.remove(text_form)
                else:
                    added.append(node)
            if remaining or not all(_inert(node, old_names) for node in added):
                return everyone
        imported = everyone & ~self.index.by_file.get(path, 0)  # tests of other files that run this one
        for name in set(old_units) | set(new_units):
            a, b = old_units.get(name), new_units.get(name)
            if a is not None and b is not None and ast.dump(a) == ast.dump(b):
                continue
            if a is None:
                # A new top-level name: harmless unless it replaces one the module already had.
                if name in old_names:
                    return everyone
                continue
            if type(a) is not type(b) or not isinstance(a, ast.ClassDef):
                if isinstance(a, ast.ClassDef):
                    mask |= class_tests(name)
                    for sub in a.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            mask |= self.covering(path, *body_span(sub))
                    if imported:
                        return everyone
                else:
                    mask |= self.covering(path, *body_span(a))
                    if b is not None and not isinstance(b, type(a)):
                        return everyone
                    if any(line in startup for line in range(a.lineno, a.end_lineno + 1)):
                        return everyone  # runs while test modules are imported
                continue
            # The same class on both sides.
            header = lambda node: ast.dump(ast.ClassDef(name=node.name, bases=node.bases, keywords=node.keywords,
                                                        body=[], decorator_list=node.decorator_list))
            old_methods, old_body = split(a.body)
            new_methods, new_body = split(b.body)
            if header(a) != header(b) or old_body != new_body:
                if imported:
                    return everyone
                mask |= class_tests(name)
                continue
            for method in set(old_methods) | set(new_methods):
                x, y = old_methods.get(method), new_methods.get(method)
                if x is not None and y is not None and ast.dump(x) == ast.dump(y):
                    continue
                if x is None:
                    if method.startswith("test"):
                        mask |= own(name, method)
                    else:
                        # A new hook or override changes every test of the class and its subclasses.
                        if imported:
                            return everyone
                        mask |= class_tests(name)
                    continue
                if isinstance(x, ast.ClassDef):
                    if imported:
                        return everyone
                    mask |= class_tests(name)
                    continue
                mask |= self.covering(path, *body_span(x))
                if method.startswith("test"):
                    mask |= own(name, method)
                if any(line in startup for line in range(x.lineno, x.end_lineno + 1)):
                    return everyone
        return mask

    # ---- one section -----------------------------------------------------
    def resolve(self, section):
        """Returns ("plain", None), ("mask", int) or ("mask", FULL)."""
        old, new = section.old, section.new
        if section.status == "rename":
            if _bad(old) or _bad(new):
                return "mask", FULL
            gone, came = self.vanishes(old), self.appears(new)
            return "mask", FULL if gone is FULL or came is FULL else gone | came
        path = new if section.status == "add" else old or new
        if _bad(path):
            return "mask", FULL
        name = PurePosixPath(path).name
        if section.status == "mode":
            return "mask", 0
        if section.status == "add":
            if (self.root / path).exists():
                return "mask", FULL
            return "mask", self.appears(path)
        if not (self.root / path).is_file():
            return "mask", FULL
        if section.status == "delete":
            return "mask", self.vanishes(path)
        if section.binary:
            if path.endswith(_IMPORT_SUFFIXES):
                return "mask", FULL
            return "mask", self.readers(path)
        if not section.hunks:
            return "mask", 0
        if name in _HOOK_NAMES:
            return "mask", FULL
        if name in _CONFIG_NAMES:
            return "mask", self.config(path, section.text)
        if not path.endswith(".py"):
            if self.reads.get(path, 0):
                return "plain", None
            return "mask", self.readers(path)
        if path.startswith("sqlglot/"):
            return "plain", None
        if not self.known(path):
            # Python that no test imports or runs (packaging, docs, benchmarks): only readers matter.
            return "mask", self.readers(path)
        if path.startswith("tests/"):
            from impact import _one, parse_patch
            try:
                parts = parse_patch(section.text)
                items = [item for part in parts for item in _one(part, self.root)]
            except Exception:
                items = None
            startup = self.index.profile["startup_body"].get(path, ())
            if items is not None and all(
                    item.kind == "equivalent" or (item.kind == "body" and not item.lines.intersection(startup))
                    for item in items):
                return "plain", None
            return "mask", self.test_module(path, section.text)
        return "plain", None


def resolve(text, index, root):
    """Split a patch into the part the ordinary analysis handles and a set of tests for the rest.

    Returns (plain_text, mask); mask is FULL when some section cannot be judged.
    """
    parts = sections(text)
    if parts is None:
        return text, 0
    resolver = Resolver(index, root)
    plain, mask = [], 0
    for section in parts:
        try:
            kind, value = resolver.resolve(section)
        except Exception:
            kind, value = "mask", FULL
        if kind == "plain":
            plain.append(section.text)
        elif value is FULL:
            return "", FULL
        else:
            mask |= value
    return "".join(plain), mask
