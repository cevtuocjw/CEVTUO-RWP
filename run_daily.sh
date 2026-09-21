#!/bin/bash
# 每日自动/开机补跑(launchd 调用,兼容 macOS 自带 bash3.2)
# 约定:当天09:00前开过机 → 今天不自动生成,等手动;
#       9点后才开机(且今天还没生成)→ 自动补一次。
cd "$(dirname "$0")" || exit 1
mkdir -p logs output state history
TODAY=$(date +%F); HOUR=$(date +%H)
NOAUTO="state/noauto_$TODAY"
run() { echo "[$(date '+%F %T')] $*" >> logs/run.log; }

if [ "$HOUR" -lt 9 ] && [ ! -f "$NOAUTO" ]; then
    touch "$NOAUTO"
    run "9点前已开机,今天改为手动生成。"
    exit 0
fi
if [ -f "$NOAUTO" ]; then
    run "今天9点前开过机,跳过自动生成。"
    exit 0
fi
if [ -f "history/$TODAY.json" ]; then
    run "$TODAY 已生成过,跳过。"
    exit 0
fi
if [ -d ".lock" ]; then
    run "上一次仍在运行,跳过。"
    exit 0
fi
mkdir ".lock" || exit 1
trap 'rmdir ".lock" 2>/dev/null' EXIT

EMAIL=""
if [ -f config.json ]; then
    if python3 -c "import json;c=json.load(open('config.json'));exit(0 if c.get('smtp',{}).get('host') and c.get('mail',{}).get('to') else 1)" 2>/dev/null; then
        EMAIL="--send-email"
    fi
fi
run "开始: rss2epub ${EMAIL}"
./.venv/bin/python rss2epub.py $EMAIL >> logs/run.log 2>&1
run "结束,退出码 $?"
