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


# ------------------------------------------------------------ 先问再做 ------
# 用户的要求:每天上午 9 点、下午 15 点**先问一声**,点同意才开始。
#
# ⭐ 能不能在 app 没打开时问?**能。** 叫醒本进程的是 launchd,不是用户点开 app,
#    所以进程一起来就能弹原生框,和主界面在不在毫无关系。
#    前提只有一条:当初在界面里开启过定时(那一步把 launchd 任务装进系统)。

def _as(s):
    """转成 AppleScript 字符串字面量。引号和反斜杠必须转义,否则脚本语法就错了。"""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def today_line():
    """「今天是否已经下载过、几点下的」—— 询问框和界面都要显示这句。"""
    rs = scheduler._today_runs()
    if not rs:
        return "今天还没有下载过。"
    last = rs[-1]
    n = f",生成 {last['count']} 本" if last.get("count") else ""
    if len(rs) == 1:
        return f"今天 {last['time']} 已经下载过一次{n}。"
    return f"今天已经下载过 {len(rs)} 次,最近一次是 {last['time']}{n}。"


def ask_user():
    """弹原生询问框,返回是否同意。

    ⚠️ giving up after 300 —— 没人理就当作「今天不用」。
    不加这个的话,一个弹窗会永远挂在屏幕上,而 launchd 认为任务还在跑,
    后续几次全都堵在它后面。
    """
    s = store.get_settings()
    days = int(s.get("window_days") or 1)
    msg = (f"{today_line()}\n\n现在开始下载 RSS 内容吗?\n"
           f"范围:最近 {days} 天")
    script = (f"display dialog {_as(msg)} with title {_as('CEVTUO-RWP')} "
              f'buttons {{"今天不用","开始下载"}} default button "开始下载" '
              f"with icon note giving up after 300")
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=340)
    except Exception as e:
        log("询问框弹不出来:", e)
        return False
    out = (r.stdout or "").replace(" ", "")
    if "gaveup:true" in out:
        log("等了 5 分钟没人回应,当作今天不用")
        return False
    yes = "buttonreturned:" in out and "开始下载" in out
    log("用户选择:", "开始下载" if yes else "今天不用")
    return yes


def record(count, trigger):
    """记一笔「今天几点下载过、生成了几本」。"""
    import json as _j
    p = BASE / "state" / "rss_runs.json"
    try:
        d = _j.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    k = datetime.date.today().isoformat()
    d.setdefault(k, []).append({"time": time.strftime("%H:%M"),
                                "count": count, "trigger": trigger})
    for old in sorted(d)[:-60]:              # 只留最近 60 天
        d.pop(old, None)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_j.dumps(d, ensure_ascii=False, indent=1), "utf-8")


def main():
    ask_mode = "--ask" in sys.argv
    s = store.get_settings()
    mode = s.get("schedule_mode") or "daily"
    if not s.get("schedule_enabled") or mode == "off":
        log("定时已关闭,跳过")
        return 0

    today = datetime.date.today()
    if ask_mode:
        # 到点了 —— 先问。⚠️ 这里**故意不做「今天跑过就跳过」的判断**:
        # 用户要的是「问我要不要」,不是软件替他决定。第二次(15:00)照样要问,
        # 只是询问框里会写明「今天 09:12 已经下载过一次」。
        if not ask_user():
            return 0
    else:
        # 命令行/手动触发时保留原来的「隔 N 天」判断
        every = {"daily": 1, "every2": 2, "every3": 3}.get(mode, 1)
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
            # 数一下这次真生成了几本 —— 界面和下次询问框都要显示
            n = 0
            try:
                n = len(list((BASE / "output" / today.isoformat()).glob("*.epub")))
            except Exception:
                pass
            record(n, "scheduled" if ask_mode else "manual")
            log(f"已记录:今天 {time.strftime('%H:%M')},生成 {n} 本")
        return rc
    finally:
        try:
            LOCK.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
