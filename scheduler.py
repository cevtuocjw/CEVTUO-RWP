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
LABEL = "com.cevtuo.rwp"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
# 历史上用过的 label。装新的之前要把它们卸掉,否则**两套定时会同时跑**,
# 同一天下载两遍而用户完全不知道为什么。
OLD_LABELS = ("com.cevtuo.rwp2epub", "com.rssdaily.epub")
RUNNER = paths.CODE_DIR / "run_scheduled.sh"   # 脚本跟着源码走(可能在 app 只读包里)

# 默认的两个询问时刻:上午 9 点、下午 15 点。
DEFAULT_TIMES = ({"hour": 9, "minute": 0}, {"hour": 15, "minute": 0})


def times(s=None):
    """界面上设的询问时刻。缺失/写坏都退回默认的两个。"""
    s = s if s is not None else store.get_settings()
    raw = s.get("schedule_times") or DEFAULT_TIMES
    out = []
    for t in raw:
        try:
            h, m = int(t.get("hour")), int(t.get("minute", 0))
        except Exception:
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            out.append({"hour": h, "minute": m})
    return out or list(DEFAULT_TIMES)


def _intervals(times_list):
    """StartCalendarInterval 用**数组**表达一天里的多个时刻。

    ⚠️ 不能写两个 <key>StartCalendarInterval</key> —— plist 里同名键后者覆盖前者,
    结果只剩下午那次,而这一点从文件上看不出来。
    """
    return "\n".join(
        "        <dict>\n"
        f"            <key>Hour</key><integer>{t['hour']}</integer>\n"
        f"            <key>Minute</key><integer>{t['minute']}</integer>\n"
        "        </dict>" for t in times_list)


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
        <string>--ask</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
{intervals}
    </array>
    <key>WorkingDirectory</key>
    <string>{base}</string>
    <key>StandardOutPath</key>
    <string>{base}/logs/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>{base}/logs/launchd.err.log</string>
    <!-- 开机不自动跑。用户要的是「到点先问我」,不是「一开机就下」。 -->
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
    ts = times(s)
    stale = [l for l in OLD_LABELS if is_loaded(l)]
    return {
        "label": LABEL,
        "plist": str(PLIST),
        "installed": PLIST.exists(),
        "loaded": is_loaded(),
        "enabled": bool(s.get("schedule_enabled")),
        "mode": s.get("schedule_mode"),
        "ask": True,                       # 现在一律「先问再做」
        "times": ts,
        "times_text": "、".join(f"{t['hour']:02d}:{t['minute']:02d}" for t in ts),
        "window_days": s.get("window_days"),
        "categories": s.get("schedule_categories"),
        "last_run": last_run_date(),
        "next_run": next_run_hint(s),
        "today": _today_runs(),
        "stale_jobs": stale,               # 非空说明有历史定时任务没清干净
    }


def _today_runs():
    """今天已经问过/下过几次、最近一次几点、生成了几本。

    界面和**询问框**都要显示这个 —— 用户明确要求
    「标注今天是否已经下载过,是几点下载的」。
    """
    import json as _j
    p = BASE / "state" / "rss_runs.json"
    try:
        d = _j.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    import datetime
    return list(d.get(datetime.date.today().isoformat()) or [])


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
    """下一个询问时刻。现在一天有两个点,算最近的那个。"""
    if not s.get("schedule_enabled") or s.get("schedule_mode") in (None, "off"):
        return "已关闭"
    import datetime
    now = datetime.datetime.now()
    for t in sorted(times(s), key=lambda x: (x["hour"], x["minute"])):
        at = now.replace(hour=t["hour"], minute=t["minute"],
                         second=0, microsecond=0)
        if at > now:
            return at.strftime("%Y-%m-%d %H:%M")
    nxt = (now + datetime.timedelta(days=1)).replace(
        hour=sorted(times(s), key=lambda x: (x["hour"], x["minute"]))[0]["hour"],
        minute=sorted(times(s), key=lambda x: (x["hour"], x["minute"]))[0]["minute"],
        second=0, microsecond=0)
    return nxt.strftime("%Y-%m-%d %H:%M")


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
    ts = times(s)
    PLIST.write_text(PLIST_TMPL.format(
        label=LABEL, runner=str(RUNNER), base=str(BASE),
        intervals=_intervals(ts)), encoding="utf-8")
    _launchctl("load", str(PLIST))
    if not is_loaded():
        _launchctl("bootstrap", f"gui/{_uid()}", str(PLIST))
    return status()


def unload_all():
    for lbl in (LABEL,) + tuple(OLD_LABELS):
        p = Path.home() / "Library/LaunchAgents" / f"{lbl}.plist"
        if is_loaded(lbl):
            _launchctl("unload", str(p))
            if is_loaded(lbl):
                _launchctl("bootout", f"gui/{_uid()}/{lbl}")
        # ⚠️ 光 unload 不够:plist 文件还躺在 LaunchAgents 里,
        #    下次登录会被重新加载 —— 于是"卸掉了"却自己回来了。
        if lbl != LABEL and p.exists():
            try:
                p.unlink()
                log.info("已删除历史定时任务 %s", p)
            except Exception as e:
                log.warning("删不掉 %s: %s", p, e)
    return True


def disable():
    store.set_settings({"schedule_enabled": False, "schedule_mode": "off"})
    return apply()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(apply(), ensure_ascii=False, indent=2))
