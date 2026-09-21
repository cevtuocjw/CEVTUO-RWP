# -*- coding: utf-8 -*-
"""定时任务管理:把界面上的设置写成 launchd plist 并加载。

launchd 只负责"每天在某个时刻叫醒一次",真正的"隔几天才做一次"由
run_scheduled.py 读取 settings 判断 —— 因为 StartCalendarInterval 表达不了
"每两天"。
"""
import json
import logging
import subprocess
import sys
from pathlib import Path

import store

log = logging.getLogger("scheduler")

import paths

BASE = paths.ensure()                       # 数据目录(日志写这里)
LABEL = "com.cevtuo.rwp2epub"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
OLD_LABEL = "com.rssdaily.epub"
OLD_PLIST = Path.home() / "Library/LaunchAgents" / f"{OLD_LABEL}.plist"
RUNNER = paths.CODE_DIR / "run_scheduled.sh"   # 脚本跟着源码走(可能在 app 只读包里)

PLIST_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{runner}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{hour}</integer>
        <key>Minute</key>
        <integer>{minute}</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>{base}</string>
    <key>StandardOutPath</key>
    <string>{base}/logs/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>{base}/logs/launchd.err.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""


def _uid():
    import os
    return os.getuid()


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def is_loaded(label=LABEL):
    r = _launchctl("list")
    return label in (r.stdout or "")


def status():
    s = store.get_settings()
    return {
        "label": LABEL,
        "plist": str(PLIST),
        "installed": PLIST.exists(),
        "loaded": is_loaded(),
        "enabled": bool(s.get("schedule_enabled")),
        "mode": s.get("schedule_mode"),
        "hour": s.get("schedule_hour"),
        "minute": s.get("schedule_minute"),
        "window_days": s.get("window_days"),
        "categories": s.get("schedule_categories"),
        "last_run": last_run_date(),
        "next_run": next_run_hint(s),
        "old_job_loaded": is_loaded(OLD_LABEL),
    }


def last_run_date():
    p = BASE / "state" / "last_scheduled_run"
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


def mark_ran(date_str):
    p = BASE / "state" / "last_scheduled_run"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(date_str, encoding="utf-8")


def next_run_hint(s):
    if not s.get("schedule_enabled") or s.get("schedule_mode") in (None, "off"):
        return "已关闭"
    import datetime
    now = datetime.datetime.now()
    today = now.date()
    at = now.replace(hour=int(s.get("schedule_hour") or 9),
                     minute=int(s.get("schedule_minute") or 0),
                     second=0, microsecond=0)
    last = last_run_date()
    try:
        last_d = datetime.date.fromisoformat(last) if last else None
    except Exception:
        last_d = None
    every = {"daily": 1, "every2": 2, "every3": 3}.get(
        s.get("schedule_mode") or "daily", 1)
    if last_d is None or (today - last_d).days >= every:
        return at.isoformat() if at > now else "随时(下次唤醒时)"
    nxt = last_d + datetime.timedelta(days=every)
    return datetime.datetime.combine(nxt, at.time()).isoformat()


def apply(settings=None):
    """按当前设置写入并加载 launchd 任务。"""
    s = settings or store.get_settings()
    enabled = bool(s.get("schedule_enabled")) and \
        (s.get("schedule_mode") or "daily") != "off"

    # 先卸掉旧的(含历史遗留的 rssdaily 任务,避免两套同时跑)
    unload_all()

    if not enabled:
        if PLIST.exists():
            PLIST.unlink()
        return {"installed": False, "loaded": False, "reason": "已关闭定时"}

    RUNNER.chmod(0o755)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(PLIST_TMPL.format(
        label=LABEL, runner=str(RUNNER), base=str(BASE),
        hour=int(s.get("schedule_hour") or 9),
        minute=int(s.get("schedule_minute") or 0)), encoding="utf-8")
    _launchctl("load", str(PLIST))
    if not is_loaded():
        _launchctl("bootstrap", f"gui/{_uid()}", str(PLIST))
    return status()


def unload_all():
    for lbl, p in ((LABEL, PLIST), (OLD_LABEL, OLD_PLIST)):
        if is_loaded(lbl):
            _launchctl("unload", str(p))
            if is_loaded(lbl):
                _launchctl("bootout", f"gui/{_uid()}/{lbl}")
    return True


def disable():
    store.set_settings({"schedule_enabled": False, "schedule_mode": "off"})
    return apply()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(apply(), ensure_ascii=False, indent=2))
