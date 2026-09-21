#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时入口:读界面上的设置,决定这次要不要跑、跑哪些分类、取几天内容。

launchd 每天在设定时刻叫一次;「隔 N 天」的间隔在这里判断
(StartCalendarInterval 表达不了这种间隔)。
用 sys.executable 调 rss2epub.py,所以在自带运行时的打包形态下也能正确工作。
"""
import datetime
import subprocess
import sys
import time
from pathlib import Path

import paths
import scheduler
import store

BASE = paths.ensure()
LOCK = BASE / ".lock_scheduled"


def log(*a):
    print(time.strftime("[%F %T]"), *a, flush=True)


def main():
    s = store.get_settings()
    mode = s.get("schedule_mode") or "daily"
    if not s.get("schedule_enabled") or mode == "off":
        log("定时已关闭,跳过")
        return 0

    every = {"daily": 1, "every2": 2, "every3": 3}.get(mode, 1)
    today = datetime.date.today()
    last = scheduler.last_run_date()
    if last:
        try:
            gap = (today - datetime.date.fromisoformat(last)).days
            if gap < every:
                log(f"上次 {last}({gap} 天前) < 间隔 {every} 天,跳过")
                return 0
        except Exception:
            pass

    if LOCK.exists():
        try:
            if time.time() - LOCK.stat().st_mtime < 6 * 3600:
                log("上一次仍在运行,跳过")
                return 0
        except Exception:
            pass
    LOCK.write_text(str(time.time()), encoding="utf-8")
    try:
        hours = int(s.get("window_days") or 1) * 24
        cats = [c for c in (s.get("schedule_categories") or []) if c]
        cmd = [sys.executable, str(paths.CODE_DIR / "rss2epub.py"),
               "--opml", str(paths.seed("feeds.opml")),
               "--output", str(BASE / "output"),
               "--state", str(BASE / "state"),
               "--history", str(BASE / "history"),
               "--log", str(BASE / "logs" / "rss2epub.log"),
               "--hours", str(hours),
               "--max-per-feed", str(int(s.get("max_per_feed") or 15))]
        if cats:
            cmd += ["--category", ",".join(cats)]
        if s.get("send_email"):
            cmd.append("--send-email")
        log("开始:", " ".join(cmd))
        rc = subprocess.run(cmd, cwd=str(paths.CODE_DIR)).returncode
        log("结束,退出码", rc)
        if rc == 0:
            scheduler.mark_ran(today.isoformat())
        return rc
    finally:
        try:
            LOCK.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
