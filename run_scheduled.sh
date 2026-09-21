#!/bin/bash
# launchd 调用本脚本;是否真的生成由 run_scheduled.py 按设置判断。
# 运行时优先用自带的(python/ + pylibs/),开发目录里退回到 .venv。
cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"

if [ -x "$DIR/python/bin/python3" ]; then
    PY="$DIR/python/bin/python3"
    export PYTHONPATH="$DIR/pylibs"
elif [ -x "$DIR/.venv/bin/python" ]; then
    PY="$DIR/.venv/bin/python"
else
    PY="$(command -v python3)"
fi

[ -x "$PY" ] || { echo "找不到 Python 运行时"; exit 1; }
exec "$PY" "$DIR/run_scheduled.py"
