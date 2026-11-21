"""Index simple module constants whose visible reads occur inside function bodies."""

from __future__ import annotations

import ast
import gzip
import importlib.util
import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from impact import _source, apply_file

INDEX = Path(__file__).with_name("scalars.json")
_WORD = re.compile(rb"(?<![\w])[A-Z_][A-Z_0-9]{2,}(?![\w])")


def _literal_assignment(node):
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and type(node.value.value) in (bool, int, float, str, bytes, type(None))
    ):
        return node.targets[0].id
    return None


def _module_reads(tree, text):
    # Local dynamic namespace access makes the use list uncertain.
    if re.search(
        r"\b(?:globals|locals|vars|eval|exec)\s*\(|__dict__|__globals__|__getattr__|sys\.modules",
        text,
    ):
        return {}
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    accesses = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            accesses[node.id].append(node)
    result = {}
    for definition in tree.body:
        name = _literal_assignment(definition)
        if not name or not re.fullmatch("[A-Z_][A-Z0-9_]{2,}", name):
            continue
        nodes = accesses[name]
        # Reject string lookup keys, dotted attributes, imports, and textual aliases.
        if len(re.findall(r"\b" + re.escape(name) + r"\b", text)) != len(nodes):
            continue
        lines = set()
        for node in nodes:
            if node is definition.targets[0]:
                continue
            if not isinstance(node.ctx, ast.Load):
                break
            # Defaults, decorators, class bodies and module-level comprehensions execute
            # before an individual test can own a line of coverage.
            child = node
            is_runtime = False
            while (parent := parents.get(child)) is not None:
                if isinstance(parent, ast.ClassDef):
                    break
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    is_runtime = child in parent.body
                    break
                child = parent
            if not is_runtime:
                break
            lines.add(node.lineno)
        else:
            if lines:
                result[name] = sorted(lines)
    return result


def build(root: Path):
    candidates = {}
    mentions = defaultdict(set)
    escaped = set()
    wildcard_packages = set()
    for base in ("sqlglot", "tests"):
        for path in sorted((root / base).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            raw = path.read_bytes()
            if path.suffix == ".gz":
                try:
                    raw = gzip.decompress(raw)
                except (ValueError, OSError, EOFError):
                    continue
            relative = path.relative_to(root).as_posix()
            for token in set(_WORD.findall(raw)):
                mentions[token.decode()].add(relative)
            if path.suffix != ".py":
                continue
            text = raw.decode("utf-8")
            tree = ast.parse(text)
            module = (
                relative.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            )
            parent = module if path.name == "__init__.py" else module.rpartition(".")[0]
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    escaped.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    dest = node.module or ""
                    if node.level:
                        dest = importlib.util.resolve_name(
                            "." * node.level + dest, parent
                        )
                    if any(a.name == "*" for a in node.names):
                        wildcard_packages.add(dest)
                    escaped.update(f"{dest}.{a.name}" for a in node.names)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value.startswith("sqlglot."):
                        escaped.add(node.value)
                    elif (
                        node.value.startswith(".")
                        and node.value.lstrip(".").isidentifier()
                    ):
                        try:
                            escaped.add(importlib.util.resolve_name(node.value, parent))
                        except ImportError:
                            pass
            if base == "sqlglot" and path.name != "__init__.py":
                candidates[relative] = (module, _module_reads(tree, text))
    results = {}
    for path, (module, constants) in candidates.items():
        if module not in escaped and not any(
            module == star or module.startswith(star + ".") for star in wildcard_packages
        ):
            usable = {
                n: lines for n, lines in constants.items() if mentions[n] == {path}
            }
            if usable:
                results[path] = usable
    return results


@lru_cache(maxsize=1)
def _load():
    try:
        return json.loads(INDEX.read_bytes())
    except (OSError, ValueError):
        return {}


def scalar_mask(item, filepatch, root, profile):
    """Return a mask for an unchanged binding whose sole edit replaces its scalar value."""
    if (
        item.kind != "binding"
        or item.scope
        or len(item.names) != 1
        or not filepatch
        or filepatch.path != item.path
        or "startup_names" not in profile
    ):
        return None
    (name,) = item.names
    lines = _load().get(item.path, {}).get(name)
    if not lines or name in profile["startup_names"]:
        return None
    if set(lines) & profile["startup"].get(item.path, set()):
        return None
    original, old, _ = _source(root / item.path)
    changed, _ = apply_file(original, filepatch)
    new = ast.parse(changed)
    if len(old.body) != len(new.body):
        return None
    matched = False
    for a, b in zip(old.body, new.body):
        if _literal_assignment(a) == name and _literal_assignment(b) == name:
            if type(a.value.value) is not type(b.value.value):
                return None
            matched = True
        elif ast.dump(a) != ast.dump(b):
            return None
    if not matched:
        return None
    mask = (
        profile["names"].get(name, 0)
        | profile["reads"].get(item.path, 0)
        | profile["incomplete"]
    )
    observed = profile["lines"].get(item.path, {})
    for line in lines:
        mask |= observed.get(line, 0)
    return mask


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    options.output.write_text(json.dumps(build(options.source), sort_keys=True) + "\n")
