# fsdeps Python-level hooks. Loaded via PYTHONPATH in every Python process of the tree
# (pytest, subprocess children, multiprocessing spawn workers). Appends JSONL records to
# $FSDEPS_OUT. Record kinds: start, list, open, fail_import, subproc.
import os
import sys

_OUT = os.environ.get("FSDEPS_OUT")
_REPO = os.environ.get("FSDEPS_REPO", "/work/tree")

if _OUT:
    import json as _json

    _pid = os.getpid()
    _fh = open(_OUT, "a", buffering=1, encoding="utf-8")
    _stdlib = os.path.dirname(os.__file__)  # /usr/local/lib/python3.13
    _site = os.path.join(_stdlib, "site-packages")
    _self = __file__
    _busy = [False]

    def _emit(rec):
        rec["pid"] = _pid
        try:
            _fh.write(_json.dumps(rec, default=repr) + "\n")
        except Exception:
            pass

    _emit({"k": "start", "argv": list(sys.argv), "ppid": os.getppid(), "exe": sys.executable})

    def _stack(f, n=10):
        out = []
        while f is not None and len(out) < n:
            out.append(f"{f.f_code.co_filename}:{f.f_lineno}:{f.f_code.co_name}")
            f = f.f_back
        return out

    def _classify(f):
        """Innermost frame that is not stdlib-proper decides: import / pytest / other."""
        while f is not None:
            fn = f.f_code.co_filename
            if fn == _self:
                f = f.f_back
                continue
            if fn.startswith("<frozen importlib") or "/importlib/" in fn or fn.startswith("<frozen zipimport"):
                return "import"
            if fn.startswith(_site):
                rel = fn[len(_site) + 1:]
                top = rel.split("/", 1)[0]
                if top in ("_pytest", "pytest", "pluggy", "py.py", "pytest_timeout.py", "xdist",
                           "iniconfig", "_distutils_hack"):
                    return "pytest"
                return "site:" + top
            if fn.startswith(_stdlib) or fn.startswith("<frozen"):
                f = f.f_back
                continue
            return "other"
        return "other"

    _LIST_EVENTS = {"os.listdir", "os.scandir", "glob.glob", "glob.glob/2", "os.walk", "os.fwalk",
                    "pathlib.Path.glob", "pathlib.Path.rglob"}

    def _hook(event, args):
        if _busy[0]:
            return
        if event == "open":
            p = args[0]
            if not isinstance(p, str):
                return
            if not p.startswith("/"):
                try:
                    p = os.path.join(os.getcwd(), p)
                except Exception:
                    return
            if not p.startswith(_REPO) or p.endswith(".pyc") or "__pycache__" in p:
                return
            _busy[0] = True
            try:
                f = sys._getframe(1)
                cls = _classify(f)
                # .py files: keep only reads that are neither imports nor pytest's own (rewrite/linecache)
                if p.endswith(".py") and cls in ("import", "pytest"):
                    return
                _emit({"k": "open", "path": p, "mode": args[1], "cls": cls, "stack": _stack(f)})
            finally:
                _busy[0] = False
            return
        if event in _LIST_EVENTS:
            _busy[0] = True
            try:
                p = args[0] if args else None
                if isinstance(p, int):
                    p = f"<fd {p}>"
                elif p is None:
                    p = "."
                elif not isinstance(p, str):
                    try:
                        p = os.fsdecode(p)
                    except Exception:
                        p = repr(p)
                ap = p if (p.startswith("/") or p.startswith("<")) else os.path.join(os.getcwd(), p)
                f = sys._getframe(1)
                _emit({"k": "list", "ev": event, "path": ap, "args": [repr(a)[:200] for a in args],
                       "cls": _classify(f), "stack": _stack(f)})
            finally:
                _busy[0] = False
            return
        if event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork"):
            _busy[0] = True
            try:
                a = list(args)
                f = sys._getframe(1)
                _emit({"k": "subproc", "ev": event, "args": [repr(x)[:500] for x in a[:3]], "stack": _stack(f, 8)})
            finally:
                _busy[0] = False

    sys.addaudithook(_hook)

    class _FailFinder:
        """Last meta_path finder: reached only when no other finder found a spec."""

        @staticmethod
        def find_spec(fullname, path=None, target=None):
            if not _busy[0]:
                _busy[0] = True
                try:
                    f = sys._getframe(1)
                    _emit({"k": "fail_import", "name": fullname,
                           "path": [str(x) for x in path] if path else None, "stack": _stack(f, 14)})
                finally:
                    _busy[0] = False
            return None

        @staticmethod
        def invalidate_caches():
            pass

    sys.meta_path.append(_FailFinder)
