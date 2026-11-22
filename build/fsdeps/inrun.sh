#!/bin/sh
# In-container: fresh copy of the pinned tree at /work/tree (as farm/lab/runner.py does),
# then pytest under strace with the sitecustomize hooks. Usage: inrun.sh KEY [pytest target...]
set -u
KEY="$1"; shift
python - <<'EOF'
import os, shutil
tree = "/work/tree"
shutil.copytree("/opt/sqlglot-pristine", tree, symlinks=True)
for dp, dn, fn in os.walk(tree):
    os.chmod(dp, 0o755)
    for f in fn:
        p = os.path.join(dp, f)
        if not os.path.islink(p):
            os.chmod(p, 0o644)
EOF
cd /work/tree
export SKIP_INTEGRATION=0
export PYTHONPATH=/work/tree:/fs/hook
export FSDEPS_OUT="/out/$KEY.jsonl"
export FSDEPS_REPO=/work/tree
rm -f "/out/$KEY.jsonl" "/out/$KEY.strace"
strace -f -qq -s 1024 -y -e signal=none \
  -e trace=%file,getdents64,execve,chdir,fchdir \
  -o "/out/$KEY.strace" \
  python -m pytest "$@" -p no:cacheprovider -q -o console_output_style=classic > "/out/$KEY.log" 2>&1
echo $? > "/out/$KEY.rc"
chown "${HOST_UID:-1000}:${HOST_GID:-1000}" /out/"$KEY".* 2>/dev/null || true
