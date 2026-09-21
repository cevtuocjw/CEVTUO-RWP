#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 Web 管理界面(每日 RSS→EPUB)。

启动: ./.venv/bin/python ui/app.py [--port 8611] [--open]
管理:今天/历史每一天的书、一键生成、每源统计(折叠面板)、打开文件位置。
"""
import argparse
import glob
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CODE = Path(__file__).resolve().parent.parent      # 代码目录(脚本所在)
UI_DIR = Path(__file__).resolve().parent
BASE = CODE                                          # 数据目录(output/state/history)
_busy = False
_child = None
_child_cat = None
_lock = threading.Lock()


def log(*a):
    print(*a, flush=True)


def json_resp(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def tail(path, n=50):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _open_path(handler):
    q = parse_qs(urlparse(handler.path).query)
    target = q.get("target", [""])[0]
    read = q.get("read", ["0"])[0] == "1"
    if not target or not os.path.exists(target):
        json_resp(handler, {"ok": False, "err": "路径不存在"})
        return
    if os.path.isfile(target):
        subprocess.Popen(["open", target] if read else ["open", "-R", target])
    else:
        subprocess.Popen(["open", target])
    json_resp(handler, {"ok": True, "target": target})


def _write_progress(obj):
    try:
        p = BASE / "state" / "progress.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _make_cmd(category):
    logf = BASE / "logs" / "ui_run.log"
    logf.parent.mkdir(parents=True, exist_ok=True)
    state = BASE / "state"
    state.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(CODE / "rss2epub.py"),
           "--output", str(BASE / "output"),
           "--state", str(state),
           "--history", str(BASE / "history"),
           "--refresh",
           "--log", str(logf)]
    if category:
        cmd += ["--category", category]
    cpath = BASE / "config.json"
    if cpath.exists():
        try:
            _c = json.loads(cpath.read_text(encoding="utf-8"))
            if _c.get("smtp", {}).get("host") and _c.get("mail", {}).get("to"):
                cmd.append("--send-email")
        except Exception:
            pass
    return cmd


def _write_progress(obj):
    try:
        p = BASE / "state" / "progress.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def generate_now(category=None):
    """后台跑一次生成。已在跑时返回 False。"""
    global _busy, _child, _child_cat
    if _busy:
        return False
    _busy, _child, _child_cat = True, None, category

    def run():
        global _busy, _child
        try:
            cmd = _make_cmd(category)
            _write_progress({"running": True, "stage": "启动", "done": 0,
                             "total": 0,
                             "message": f"{category or '全部'} 生成中…"})
            _child = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            rc = _child.wait()
            ok = (rc == 0)
            _write_progress({"running": False, "stage": "完成", "done": 0,
                             "total": 0, "ok": ok,
                             "message": "完成" if ok else "失败: 见下方日志"})
        except Exception as e:
            _write_progress({"running": False, "stage": "完成", "done": 0,
                             "total": 0, "ok": False, "message": f"失败: {e}"})
        finally:
            _busy = False
            _child = None

    threading.Thread(target=run, daemon=True).start()
    return True


def force_generate(category=None):
    """一键重刷:清掉任何残留运行/卡死状态后重新生成。"""
    global _busy, _child
    # 杀掉可能残留的生成进程与测试 Chrome
    for pat in ("rss2epub.py --refresh", "remote-debugging-port=9334"):
        try:
            subprocess.run(["pkill", "-f", pat], capture_output=True)
        except Exception:
            pass
    if _child is not None:
        try:
            _child.terminate()
        except Exception:
            pass
    _busy = False
    _child = None
    _write_progress({"running": False, "stage": "空闲", "ok": None,
                     "message": "已清理,重新开始"})
    return generate_now(category)


def _book_list(date):
    out = BASE / "output" / date
    if not out.exists():
        return []
    return [{"name": p.name, "size": p.stat().st_size,
             "mtime": p.stat().st_mtime, "path": str(p)}
            for p in sorted(out.glob("*.epub"))]


def _report(date):
    p = BASE / "history" / f"{date}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send_static(self):
        up = urlparse(self.path)
        if up.path in ("/", "/index.html"):
            f = UI_DIR / "index.html"
        else:
            f = UI_DIR / up.path.lstrip("/")
            f = f.resolve()
            if not str(f).startswith(str(UI_DIR.resolve())):
                self.send_error(404)
                return
        if f.exists():
            ctype = mimetypes.guess_type(f.name)[0] or "text/html"
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_GET(self):
        up = urlparse(self.path)
        route = up.path
        try:
            if route == "/api/dates":
                files = sorted(glob.glob(str(BASE / "history" / "*.json")),
                               reverse=True)
                dates = [Path(f).stem for f in files]
                json_resp(self, {"dates": dates})
            elif route == "/api/report":
                date = parse_qs(up.query).get("date", [""])[0]
                r = _report(date) if date else None
                json_resp(self, r if r else {"err": "not found"}, 200 if r else 404)
            elif route == "/api/books":
                date = parse_qs(up.query).get("date", [""])[0]
                json_resp(self, {"books": _book_list(date)})
            elif route == "/api/status":
                p = BASE / "state" / "progress.json"
                prog = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
                prog["running"] = _busy
                json_resp(self, prog)
            elif route == "/api/logtail":
                n = int(parse_qs(up.query).get("n", ["40"])[0])
                json_resp(self, {"lines": tail(BASE / "logs" / "ui_run.log", n)})
            elif route == "/api/open":
                _open_path(self)
            else:
                self._send_static()
        except Exception as e:
            json_resp(self, {"err": str(e)}, 500)

    def do_POST(self):
        up = urlparse(self.path)
        if up.path in ("/api/generate", "/api/force"):
            cat = parse_qs(up.query).get("cat", [""])[0] or None
            ok = force_generate(cat) if up.path == "/api/force" else generate_now(cat)
            json_resp(self, {"started": ok, "category": cat, "force": up.path == "/api/force"},
                      200 if ok else 409)
        else:
            json_resp(self, {"err": "unknown"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8611)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--data", default=str(CODE), help="数据目录(output/state/history 所在)")
    args = ap.parse_args()
    global BASE
    BASE = Path(args.data)
    _write_progress({"running": False, "stage": "空闲", "ok": True,
                     "message": "就绪"})      # 清掉上次中断留下的卡死状态
    url = f"http://127.0.0.1:{args.port}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        # 已有一个实例在跑:不用重复起,直接打开它的页面即可
        log("端口被占用:已有一个管理实例在运行,直接打开…")
        if args.open:
            subprocess.Popen(["open", url])
        return
    log(f"RSS 管理界面: {url}")
    if args.open:
        threading.Timer(0.8, partial(subprocess.Popen, ["open", url])).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
