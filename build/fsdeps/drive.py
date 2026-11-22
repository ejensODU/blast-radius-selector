#!/usr/bin/env python3
"""Run inrun.sh once per test file (and once for the whole suite) in rwx-fstrace:1 containers.

usage: drive.py [--jobs 4] [--force] [--only KEY...]
Raw outputs land in raw/<key>.{strace,jsonl,log,rc}; then run parse.py.
"""
import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent.parent / "kit" / "sample" / "tests.json"
IMAGE = "rwx-fstrace:1"


def run(key, target):
    raw = HERE / "raw"
    if (raw / f"{key}.rc").exists() and not ARGS.force:
        return key, "cached", 0
    cmd = ["docker", "run", "--rm", "--network", "none", "--cpus", "2", "--memory", "4096m",
           "--memory-swap", "4096m", "--pids-limit", "512", "--cap-add", "SYS_PTRACE",
           "--security-opt", "seccomp=unconfined", "-e", f"HOST_UID={os.getuid()}", "-e", f"HOST_GID={os.getgid()}",
           "-v", f"{HERE}:/fs:ro", "-v", f"{raw}:/out", IMAGE, "sh", "/fs/inrun.sh", key, *target]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    return key, f"docker_rc={p.returncode} {p.stderr[-300:]}", time.time() - t0


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", nargs="*")
    ARGS = ap.parse_args()
    (HERE / "raw").mkdir(exist_ok=True)
    subprocess.run(["chcon", "-R", "-t", "container_file_t", str(HERE)], check=False)
    files = sorted({t["file"] for t in json.loads(TESTS.read_text())["tests"]})
    # test_*.py files present in the tree but absent from tests.json (0 tests under pytest)
    files += ["tests/test_docs.py", "tests/test_integration_loader.py"]
    jobs = [("whole-suite", [])] + [(f.replace("/", "__"), [f]) for f in files]
    if ARGS.only:
        jobs = [j for j in jobs if j[0] in ARGS.only]
    with cf.ThreadPoolExecutor(ARGS.jobs) as ex:
        for key, msg, dt in ex.map(lambda j: run(*j), jobs):
            print(f"{key}: {msg} {dt:.0f}s", flush=True)


if __name__ == "__main__":
    main()
