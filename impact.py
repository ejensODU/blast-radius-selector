"""Locate changed executable code in the immutable baseline checkout."""
from __future__ import annotations

import ast
import dis
import re
import shlex
import types
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

_FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_SCOPE = (*_FUNCTION, ast.ClassDef)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_LOGIC = re.compile(r"\b(?:and|or|if|else)\b")


@dataclass(frozen=True)
class Impact:
    path: str
    kind: str
    lines: frozenset[int] = frozenset()
    names: frozenset[str] = frozenset()
    scope: tuple[str, ...] = ()
    function_lines: tuple[int, int] | None = None
    keys: frozenset[str] = frozenset()


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]


@dataclass
class FilePatch:
    path: str
    old_path: str
    hunks: list[Hunk]
    valid: bool = True


def _path(header: str) -> str:
    header = header.rstrip("\r\n").split("\t", 1)[0]
    if header.startswith('"'):
        words = shlex.split(header)
        header = words[0] if len(words) == 1 else ""
    if header[:2] in ("a/", "b/"):
        header = header[2:]
    return header


def parse_patch(patch_text: str) -> list[FilePatch]:
    """Read unified diffs. Validate declared hunk lengths before applying."""
    result = []
    current = None
    hunk = None
    lines = iter(patch_text.splitlines(keepends=True))
    for line in lines:
        if line.startswith("diff --git "):
            current, hunk = None, None
            try:
                names = shlex.split(line)
                if len(names) == 4:
                    current = FilePatch(_path(names[3]), _path(names[2]), [])
            except ValueError:
                pass
            if current is None:
                current = FilePatch("", "", [], False)
            result.append(current)
        elif line.startswith("--- "):
            old_path = _path(line[4:])
            new_line = next(lines, "")
            if current is None or current.hunks:
                current = FilePatch("", old_path, [])
                result.append(current)
            hunk = None
            if new_line.startswith("+++ "):
                new_path = _path(new_line[4:])
                current.old_path = old_path
                current.path = old_path if new_path == "/dev/null" else new_path
                current.valid &= new_path != "/dev/null" and old_path == new_path
            else:
                current.valid = False
        elif (match := _HUNK.match(line)) and current:
            hunk = Hunk(int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1), [])
            current.hunks.append(hunk)
            old_seen = new_seen = 0
            while old_seen < hunk.old_count or new_seen < hunk.new_count:
                item = next(lines, "")
                if item.startswith("\\ No newline") and hunk.lines:
                    hunk.lines[-1] = hunk.lines[-1].removesuffix("\n")
                    continue
                if not item or item[0] not in " +-":
                    current.valid = False
                    break
                hunk.lines.append(item)
                old_seen += item[0] in " -"
                new_seen += item[0] in " +"
            current.valid &= (old_seen, new_seen) == (hunk.old_count, hunk.new_count)
        elif line.startswith("\\ No newline") and hunk and hunk.lines:
            hunk.lines[-1] = hunk.lines[-1].removesuffix("\n")
    return result


@dataclass
class Change:
    line: int
    before: list[str]
    after: list[str]


def apply_file(original: str, patch: FilePatch) -> tuple[str, list[Change]]:
    """Apply a patch in memory, reporting contiguous edit blocks at baseline lines."""
    baseline = original.splitlines(keepends=True)
    result: list[str] = []
    changes = []
    cursor = 0
    for hunk in patch.hunks:
        start = hunk.old_start - bool(hunk.old_count)
        if not cursor <= start <= len(baseline):
            raise ValueError("Invalid hunk offset")
        result.extend(baseline[cursor:start])
        cursor = start
        block = None
        for item in hunk.lines:
            kind, value = item[0], item[1:]
            if kind != " " and block is None:
                block = Change(cursor + 1, [], [])
                changes.append(block)
            if kind in " -":
                if cursor >= len(baseline) or value != baseline[cursor]:
                    raise ValueError("Hunk disagrees with pinned baseline")
                cursor += 1
            if kind == "-":
                block.before.append(value)
            elif kind == "+":
                block.after.append(value)
                result.append(value)
            else:
                block = None
                result.append(value)
    result.extend(baseline[cursor:])
    return "".join(result), changes


@lru_cache(maxsize=64)
def _source(path: Path):
    original = path.read_text(encoding="utf-8")
    tree = ast.parse(original, str(path))
    compiled = compile(tree, str(path), "exec", dont_inherit=True)
    return original, tree, compiled


_FRAME_READERS = frozenset({"locals", "vars", "eval", "exec", "dir", "_getframe", "currentframe", "f_locals"})


def _same_code(left, right, loose=False):
    """Ignore source positions, preserving code, constants, symbols and exception tables.

    With `loose`, the names of plain local variables are ignored as well: they are
    slots in the frame, and renaming one consistently leaves the bytecode unchanged.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, tuple):
        return len(left) == len(right) and all(_same_code(a, b, loose) for a, b in zip(left, right))
    if not isinstance(left, types.CodeType):
        # repr distinguishes signed zero and typed values inside frozen constants.
        return left == right and repr(left) == repr(right)
    attrs = ("co_name", "co_qualname", "co_argcount", "co_posonlyargcount", "co_kwonlyargcount",
             "co_code", "co_names", "co_cellvars", "co_freevars", "co_flags",
             "co_exceptiontable")
    if not all(getattr(left, k) == getattr(right, k) for k in attrs):
        return False
    if left.co_varnames != right.co_varnames:
        named = left.co_argcount + left.co_kwonlyargcount + bool(left.co_flags & 4) + bool(left.co_flags & 8)
        if (not loose or len(left.co_varnames) != len(right.co_varnames)
                or left.co_varnames[:named] != right.co_varnames[:named]
                or (_names(left) | _names(right)) & _FRAME_READERS):
            return False
    return _same_code(left.co_consts, right.co_consts, loose)


def _names(code):
    found = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            found |= _names(const)
    return found


class _DropPass(ast.NodeTransformer):
    """`pass` beside other statements only leaves a no-op for the line tracer."""

    def generic_visit(self, node):
        super().generic_visit(node)
        for name in ("body", "orelse", "finalbody"):
            block = getattr(node, name, None)
            if isinstance(block, list) and len(block) > 1:
                kept = [stmt for stmt in block if not isinstance(stmt, ast.Pass)]
                if kept and len(kept) != len(block):
                    setattr(node, name, kept)
        return node


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\S")


def _rename_shaped(changes):
    """Cheap screen: do the edited lines differ only in identifiers or in `pass` lines?"""
    import keyword
    for block in changes:
        before = [line for line in block.before if line.strip() != "pass"]
        after = [line for line in block.after if line.strip() != "pass"]
        if len(before) != len(after):
            return False
        for old, new in zip(before, after):
            left, right = _WORD.findall(old), _WORD.findall(new)
            if len(left) != len(right):
                return False
            for x, y in zip(left, right):
                if x != y and not (x.isidentifier() and y.isidentifier()
                                   and not keyword.iskeyword(x) and not keyword.iskeyword(y)):
                    return False
    return True


def _refactor_only(original, modified, path, changes):
    """Same program after dropping redundant `pass` statements and local variable names."""
    if not _rename_shaped(changes):
        return False
    try:
        codes = []
        for text in (original, modified):
            tree = _DropPass().visit(ast.parse(text.encode("utf-8"), path))
            codes.append(compile(ast.fix_missing_locations(tree), path, "exec", dont_inherit=True))
    except (SyntaxError, ValueError, RecursionError):
        return False
    return _same_code(codes[0], codes[1], loose=True)


def _is_doc_stmt(stmt):
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))


def _same_ast_no_doc(left, right):
    """True when two ASTs are identical aside from module/class/function docstrings."""
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_ast_no_doc(a, b) for a, b in zip(left, right))
    if not isinstance(left, ast.AST):
        return left == right and type(left) is type(right)
    has_doc = isinstance(left, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    for field in left._fields:
        lv, rv = getattr(left, field), getattr(right, field)
        if has_doc and field == "body":
            lb = lv[1:] if lv and _is_doc_stmt(lv[0]) else lv
            rb = rv[1:] if rv and _is_doc_stmt(rv[0]) else rv
            if not _same_ast_no_doc(lb, rb):
                return False
        elif not _same_ast_no_doc(lv, rv):
            return False
    return True


def _node_path(node: ast.AST, line: int, col: int):
    candidates = []
    for child in ast.iter_child_nodes(node):
        if hasattr(child, "lineno"):
            if (child.lineno, child.col_offset) <= (line, col) < (child.end_lineno, child.end_col_offset):
                candidates.append(_node_path(child, line, col))
        elif isinstance(child, (ast.arguments, ast.comprehension, ast.withitem, ast.match_case)):
            candidate = _node_path(child, line, col)
            if len(candidate) > 1:
                candidates.append(candidate)
    return [node] + max(candidates, key=len, default=[])


def _dict_keys(nodes, lineno, col):
    found = set()
    for node in nodes:
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (key is not None and (key.lineno, key.col_offset) <= (lineno, col)
                        <= (value.end_lineno, value.end_col_offset)):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        found.add(key.value)
                    elif isinstance(key, ast.Attribute):
                        found.add(key.attr)
    return frozenset(found)


def _header_lines(stmt):
    """Entry positions for continued expressions and short-circuit conditions."""
    result = {stmt.lineno}
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, ast.expr):
            result.add(child.lineno)
            while (children := [n for n in ast.iter_child_nodes(child) if isinstance(n, ast.expr)]):
                child = children[0]
                result.add(child.lineno)
    return result


def _executable(code):
    for _, line in dis.findlinestarts(code):
        if line is not None:
            yield line
    for child in code.co_consts:
        if isinstance(child, types.CodeType):
            yield from _executable(child)


def _changed_line(path, tree, executable, line, before, after):
    # AST positions count UTF-8 bytes. For a full deletion use the first nonblank.
    col = len(before) - len(before.lstrip())
    if after is not None:
        i = 0
        while i < min(len(before), len(after)) and before[i] == after[i]:
            i += 1
        # For an inserted token, use the preceding character if at end of old line.
        col = min(i, max(col, len(before.rstrip()) - 1))
    byte_col = len(before[:col].encode("utf-8"))
    nodes = _node_path(tree, line, byte_col)
    scopes = [n for n in nodes if isinstance(n, _SCOPE)]
    scope = tuple(getattr(n, "name", "<lambda>") for n in scopes)
    stmt = next((n for n in reversed(nodes) if isinstance(n, ast.stmt)), None)
    if not stmt:
        return Impact(path, "unknown", frozenset([line]))
    closest = scopes[-1] if scopes else None
    if isinstance(closest, _FUNCTION):
        if isinstance(closest, ast.Lambda):
            in_body = closest.body in nodes
        else:
            in_body = any(n in nodes for n in closest.body)
        if in_body:
            touched = {line}
            if line not in executable or (after is not None and _LOGIC.findall(before) != _LOGIC.findall(after)):
                touched.update(_header_lines(stmt))
            body = (closest.lineno, closest.end_lineno)
            return Impact(path, "body", frozenset(touched), scope=scope, function_lines=body)
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        # Renaming a class attribute or module alias can break imports. Track
        # literal/argument edits in existing bindings only.
        if stmt.value is None or stmt.value not in nodes:
            return Impact(path, "unknown", frozenset([line]), scope=scope)
        names = frozenset(n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name))
        if names:
            return Impact(path, "binding", frozenset([line]), names, scope, keys=_dict_keys(nodes, line, byte_col))
    return Impact(path, "unknown", frozenset([line]), scope=scope)


def _one(patch, root):
    path = patch.path
    if (not patch.valid or not patch.hunks or not path or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts):
        return [Impact(path, "unknown")]
    if not path.endswith(".py"):
        return [Impact(path, "fixture")]
    original, tree, code = _source(root / path)
    modified, changes = apply_file(original, patch)
    # Compiling bytes respects a changed source-encoding cookie. CPython 3.13
    # records __firstlineno__ in class constants even for ordinary comment
    # insertions. Compare position-free syntax as well for text-only changes.
    updated_tree = compile(modified.encode("utf-8"), path, "exec", ast.PyCF_ONLY_AST, dont_inherit=True)
    updated = compile(updated_tree, path, "exec", dont_inherit=True)
    text_only = all(not line.strip() or line.lstrip().startswith("#")
                    for block in changes for line in (*block.before, *block.after))
    if (_same_code(code, updated) or (text_only and ast.dump(tree) == ast.dump(updated_tree))
            or _refactor_only(original, modified, path, changes)):
        return [Impact(path, "equivalent")]
    if _same_ast_no_doc(tree, updated_tree):
        return [Impact(path, "docstring")]
    executable = set(_executable(code))
    result = []
    for change in changes:
        if not change.before:
            # Without a corresponding old instruction, old line coverage can miss an
            # inserted branch. Include the containing old function when identifiable.
            lines = original.splitlines()
            loc = change.line
            if loc < 1 or loc > len(lines):
                result.append(Impact(path, "unknown"))
                continue
            old_line = lines[loc - 1]
            anchor = _changed_line(path, tree, executable, loc, old_line, None)
            inserted = [s for s in change.after if s.strip() and not s.lstrip().startswith("#")]
            indentation = min((len(s) - len(s.lstrip()) for s in inserted), default=0)
            scopes = _node_path(tree, loc, len(old_line[:len(old_line) - len(old_line.lstrip())].encode()))
            fn = next((n for n in reversed(scopes) if isinstance(n, _FUNCTION)), None)
            if anchor.kind == "body" and fn and indentation > fn.col_offset:
                span = anchor.function_lines
                result.append(Impact(path, "body", frozenset(range(span[0], span[1] + 1)), scope=anchor.scope, function_lines=span))
            else:
                result.append(Impact(path, "unknown"))
            continue
        aligned = len(change.before) == len(change.after)
        for i, line in enumerate(change.before):
            replacement = change.after[i] if aligned else None
            if (replacement is not None
                    and len(line) - len(line.lstrip()) != len(replacement) - len(replacement.lstrip())):
                # Moving an instruction across a conditional or a declaration
                # can affect tests that never executed its old location.
                result.append(Impact(path, "unknown"))
            else:
                result.append(_changed_line(path, tree, executable, change.line + i, line, replacement))
        if change.after and not aligned:
            # An unaligned replacement can introduce effects outside the deleted lines.
            result.append(Impact(path, "unknown"))
    return result or [Impact(path, "unknown")]


def analyze(patch_text: str, source_root: Path) -> list[Impact]:
    root = Path(source_root)
    try:
        patches = parse_patch(patch_text)
    except ValueError:
        return [Impact("", "unknown")]
    result = []
    for patch in patches:
        try:
            result.extend(_one(patch, root))
        except (OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
            result.append(Impact(patch.path, "unknown"))
    return result or [Impact("", "unknown")]
