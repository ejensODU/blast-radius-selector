#!/usr/bin/env python3
"""Fold raw/<key>.{strace,jsonl,log,rc} into fsdeps.json.

usage: parse.py [--raw DIR] [--out fsdeps.json]
Keys: raw file key 'tests__dialects__test_x.py' -> 'tests/dialects/test_x.py'; 'whole-suite' -> '<whole-suite>'.
"""
import argparse
import json
import os
import posixpath
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "/work/tree"
HOST_TREE = HERE.parent.parent / "sqlglot_src"  # same pinned commit; used only to tell dirs from files
CONFIG_NAMES = {"pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg", "conftest.py"}

LINE = re.compile(r"^(\d+)\s+(\w+)\((.*)\)\s+=\s+(-?\d+|0x[0-9a-f]+|\?)(?:<(.*?)>)?(?:\s+(E[A-Z]+)\s)?")
DIRFD_ARG = re.compile(r'^(AT_FDCWD|-?\d+)(?:<([^>]*)>)?,\s*"((?:[^"\\]|\\.)*)"')
STR_ARG = re.compile(r'^"((?:[^"\\]|\\.)*)"')
FD_ARG = re.compile(r"^(\d+)<([^>]*)>")
DIRFD_CALLS = {"openat", "openat2", "newfstatat", "fstatat64", "statx", "faccessat", "faccessat2",
               "readlinkat", "mkdirat", "unlinkat", "renameat", "renameat2", "utimensat", "fchmodat",
               "fchownat", "linkat", "symlinkat", "mknodat", "name_to_handle_at", "execveat",
               "inotify_add_watch"}
PATH_CALLS = {"open", "stat", "lstat", "access", "readlink", "execve", "chdir", "mkdir", "rmdir",
              "unlink", "rename", "creat", "truncate", "chmod", "chown", "lchown", "utime", "utimes",
              "statfs", "getxattr", "lgetxattr", "listxattr", "llistxattr", "link", "symlink", "chroot",
              "mknod", "acct", "swapon", "uselib", "pivot_root"}
STATLIKE = {"newfstatat", "fstatat64", "statx", "faccessat", "faccessat2", "readlinkat", "stat", "lstat",
            "access", "readlink", "getxattr", "lgetxattr", "listxattr", "llistxattr", "statfs"}
WRITE_CALLS = {"mkdir", "mkdirat", "unlink", "unlinkat", "rename", "renameat", "renameat2", "rmdir",
               "creat", "truncate", "chmod", "fchmodat", "utime", "utimes", "utimensat", "link", "linkat",
               "symlink", "symlinkat"}


def unesc(s):
    if "\\" not in s:
        return s
    b = s.encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1")
    return b.decode("utf-8", "replace")


def ignored(rel):
    parts = rel.split("/")
    return "__pycache__" in parts or rel.endswith(".pyc") or ".pytest_cache" in parts


def rel_of(p):
    if p == REPO:
        return "."
    if p.startswith(REPO + "/"):
        return p[len(REPO) + 1:]
    return None


def join_lines(path):
    """Merge '<unfinished ...>' / '<... x resumed>' pairs (per pid)."""
    pend = {}
    with open(path, errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = re.match(r"^(\d+)\s+(.*)$", line)
            if not m:
                continue
            pid, rest = m.groups()
            if rest.endswith("<unfinished ...>"):
                pend[pid] = rest[: -len("<unfinished ...>")].rstrip()
                continue
            r = re.match(r"^<\.\.\. \w+ resumed>\s?(.*)$", rest)
            if r:
                head = pend.pop(pid, None)
                if head is None:
                    continue
                rest = head + r.group(1)
            yield f"{pid} {rest}"


def parse_strace(path):
    cwd = {}
    ev = []  # (call, abs_path, ok, errno, flags_str)
    execs = []
    for line in join_lines(path):
        m = LINE.match(line)
        if not m:
            continue
        pid, call, args, ret, retdeco, errno = m.groups()
        ok = not ret.startswith("-")
        if call == "getdents64":
            fm = FD_ARG.match(args)
            if fm:
                ev.append(("getdents64", fm.group(2), ok, errno, ""))
            continue
        if call == "fchdir":
            fm = FD_ARG.match(args)
            if fm and ok:
                cwd[pid] = fm.group(2)
            continue
        if call in DIRFD_CALLS:
            dm = DIRFD_ARG.match(args)
            if not dm:
                continue
            fd, base, p = dm.groups()
            p = unesc(p)
            if fd == "AT_FDCWD" and base:
                cwd[pid] = base
            if p == "":
                continue  # AT_EMPTY_PATH: operation on an already-open fd
            if not p.startswith("/"):
                p = posixpath.join(base or cwd.get(pid, REPO), p)
            flags = args[dm.end():]
        elif call in PATH_CALLS:
            sm = STR_ARG.match(args)
            if not sm:
                continue
            p = unesc(sm.group(1))
            if not p.startswith("/"):
                p = posixpath.join(cwd.get(pid, REPO), p)
            flags = args[sm.end():]
            if call == "chdir" and ok:
                cwd[pid] = posixpath.normpath(p)
            if call == "execve":
                am = re.match(r',\s*\[(.*?)\](?:,|$)', flags)
                argv = [unesc(x) for x in re.findall(r'"((?:[^"\\]|\\.)*)"', am.group(1))] if am else []
                execs.append({"pid": int(pid), "path": p, "argv": argv, "ok": ok})
        else:
            continue
        ev.append((call, posixpath.normpath(p), ok, errno, flags))
    return ev, execs


def isdir_host(rel):
    return (HOST_TREE / rel).is_dir()


def summarize_strace(ev):
    opened, dirs, statok, missing, written = set(), set(), set(), set(), set()
    config = {}
    for call, p, ok, errno, flags in ev:
        base = posixpath.basename(p)
        if base in CONFIG_NAMES and "__pycache__" not in p:
            config.setdefault(p, set()).add(f"{call}:{'ok' if ok else errno}")
        rel = rel_of(p)
        if rel is None or ignored(rel):
            continue
        if call == "getdents64":
            dirs.add(rel)
            continue
        if not ok:
            if errno in ("ENOENT", "ENOTDIR"):
                missing.add(rel)
            continue
        if call in ("open", "openat", "openat2", "creat"):
            if "O_DIRECTORY" in flags or isdir_host(rel):
                dirs.add(rel)
            else:
                opened.add(rel)
            if any(x in flags for x in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC")):
                written.add(rel)
        elif call in STATLIKE or call == "chdir":
            statok.add(rel)
        elif call == "execve":
            opened.add(rel)
        elif call in WRITE_CALLS:
            written.add(rel)
    stat_only = statok - opened - dirs
    # A path probed missing and later present (e.g. created by the run) stays in both lists.
    return {
        "opened": sorted(opened),
        "probed_missing": sorted(missing),
        "stat_only": sorted(stat_only),
        "dirs_opened": sorted(dirs),
        "written": sorted(written),
        "config_probes": {k: sorted(v) for k, v in sorted(config.items())},
    }


def relish(p):
    r = rel_of(posixpath.normpath(p)) if p.startswith("/") else None
    return r if r is not None else p


def summarize_jsonl(path):
    lst_non, lst_pt, fi_repo, fi_other, subs, py_opens, procs = [], [], set(), set(), [], [], []
    seen_list = set()
    if not path.exists():
        return {}
    for line in path.open(errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        k = r.get("k")
        if k == "start":
            procs.append({"pid": r["pid"], "ppid": r.get("ppid"), "argv": r.get("argv")})
        elif k == "list":
            if r["cls"] == "import":
                continue
            item = {"path": relish(r["path"]), "ev": r["ev"], "cls": r["cls"], "stack": r["stack"][:6]}
            sig = json.dumps(item, sort_keys=True)
            if sig in seen_list:
                continue
            seen_list.add(sig)
            (lst_pt if r["cls"] == "pytest" else lst_non).append(item)
        elif k == "fail_import":
            n = r["name"]
            top = n.split(".")[0]
            if "." not in n or top in ("sqlglot", "tests", "sqlglotc", "sqlglotrs"):
                fi_repo.add(n)
            else:
                fi_other.add(n)
        elif k == "subproc":
            subs.append({"ev": r["ev"], "args": r["args"], "stack": r["stack"][:4]})
        elif k == "open":
            item = {"path": relish(r["path"]), "mode": r.get("mode"), "cls": r["cls"], "stack": r["stack"][:4]}
            if item not in py_opens:
                py_opens.append(item)
    return {
        "listed_dirs_non_import": lst_non,
        "listed_dirs_pytest": lst_pt,
        "failed_imports_repo": sorted(fi_repo),
        "failed_imports_other": sorted(fi_other),
        "subprocesses": subs,
        "py_opens_non_py": py_opens,
        "python_processes": procs,
    }


def n_passed(log):
    txt = log.read_text(errors="replace") if log.exists() else ""
    tail = txt.strip().splitlines()[-1] if txt.strip() else ""
    n = 0
    for num, sub in re.findall(r"(\d+) (subtests )?passed", tail):
        if not sub:
            n += int(num)
    return n, tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(HERE / "raw"))
    ap.add_argument("--out", default=str(HERE / "fsdeps.json"))
    a = ap.parse_args()
    raw = Path(a.raw)
    out = {}
    for rc in sorted(raw.glob("*.rc")):
        key = rc.name[:-3]
        name = "<whole-suite>" if key == "whole-suite" else key.replace("__", "/")
        ev, execs = parse_strace(raw / f"{key}.strace") if (raw / f"{key}.strace").exists() else ([], [])
        rec = summarize_strace(ev)
        rec.update(summarize_jsonl(raw / f"{key}.jsonl"))
        rec["execve"] = execs
        # subprocesses: Python-level audit records (with stack) + every execve below the root
        # pytest process (catches multiprocessing spawn, which bypasses subprocess.Popen).
        rec["subprocesses"] = rec.get("subprocesses", []) + [
            {"ev": "execve", "argv": e["argv"], "ok": e["ok"]} for e in execs[1:]]
        rec["pytest_rc"] = int(rc.read_text().strip() or -1)
        rec["n_tests_passed"], rec["pytest_summary"] = n_passed(raw / f"{key}.log")
        out[name] = rec
    Path(a.out).write_text(json.dumps(out, indent=1, sort_keys=False) + "\n")
    print(f"wrote {a.out}: {len(out)} keys")


if __name__ == "__main__":
    main()
