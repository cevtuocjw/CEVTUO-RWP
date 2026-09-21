# -*- coding: utf-8 -*-
"""用 Chrome(独立档 + 注入你自己的登录 Cookie)渲染取正文。

只做:拉起一个独立用户目录的 Chrome → 把 config 里该域的 Cookie 注入(你的账号)
→ 打开文章 → 等渲染 → 读回 DOM。不调用/注入任何扩展(含 BPC)。
Chrome 会自动在后台启动/复用,无需你每次开 Chrome。
"""
import json
import os
import subprocess
import time
import urllib.parse

import requests
import websocket

log = None
PORT = 9334
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_lk = None


def _log(m):
    if log:
        log.info(m)


def _launch_chrome(profile_dir, port):
    subprocess.Popen(
        [DEFAULT_CHROME, f"--user-data-dir={profile_dir}",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         "--no-first-run", "--window-size=1280,2000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if chrome_reachable(base=f"http://127.0.0.1:{port}"):
            return True
        time.sleep(1)
    return False


def chrome_reachable(base=None, timeout=1.5):
    base = base or f"http://127.0.0.1:{PORT}"
    try:
        requests.get(base + "/json/version", timeout=timeout)
        return True
    except Exception:
        return False


def ensure_chrome(profile_dir, port):
    """启动(或确认)独立 Chrome;线程安全。"""
    global _lk
    if _lk is None:
        import threading
        _lk = threading.Lock()
    with _lk:
        if not chrome_reachable(base=f"http://127.0.0.1:{port}"):
            os.makedirs(profile_dir, exist_ok=True)
            return _launch_chrome(profile_dir, port)
        return True


def cookie_file_for(cfg, url):
    """按域名后缀找到 cfg.cookies 里最长匹配的那个文件路径。"""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    best, best_n = None, -1
    for suffix, path in ((cfg or {}).get("cookies") or {}).items():
        s = suffix.lstrip(".").lower()
        if host == s or host.endswith("." + s):
            if len(s) > best_n:
                best, best_n = path, len(s)
    return best if best and os.path.exists(best) else None


def _set_cookies(ws, cmd, cookie_path):
    arr = json.load(open(cookie_path, encoding="utf-8"))
    ok = 0
    for c in arr:
        if not c.get("name"):
            continue
        if c.get("session") is False and (c.get("expirationDate") or 0) < time.time():
            continue
        r = cmd("Network.setCookie", {"name": c["name"], "value": c.get("value", ""),
                                      "domain": c.get("domain", ""),
                                      "path": c.get("path", "/"),
                                      "secure": bool(c.get("secure"))})
        if r and r.get("result", {}).get("success"):
            ok += 1
    return ok


def render_with_cookies(url, cookie_path, cfg=None, timeout=45, port=None):
    """在独立 Chrome 里注入该域 Cookie 并渲染文章,返回 (html, ok)。"""
    cfg = cfg or {}
    profile = cfg.get("browser", {}).get("user_data") or os.path.expanduser(
        "~/Library/Application Support/RSSDailyEpub/chrome_profile")
    port = port or int(cfg.get("browser", {}).get("port") or PORT)
    if not ensure_chrome(profile, port):
        return "", False
    base = f"http://127.0.0.1:{port}"
    try:
        tgt = requests.put(base + "/json/new?about:blank", timeout=6).json()
        wsurl = tgt.get("webSocketDebuggerUrl")
        if not wsurl:
            return "", False
        ws = websocket.create_connection(wsurl, timeout=15)
        mid = [0]

        def cmd(method, params=None):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": method,
                                "params": params or {}}))
            ws.settimeout(timeout)
            while True:
                try:
                    msg = json.loads(ws.recv())
                except Exception:
                    return None
                if msg.get("id") == mid[0]:
                    return msg

        cmd("Network.enable")
        _set_cookies(ws, cmd, cookie_path)
        cmd("Page.enable")
        cmd("Runtime.enable")
        cmd("Page.navigate", {"url": url})

        # 等正文稳定
        expr = ("document.body ? document.body.innerText.length : 0")
        last, stable, deadline = -1, time.time(), time.time() + timeout
        while time.time() < deadline:
            r = cmd("Runtime.evaluate", {"expression": expr,
                                         "returnByValue": True})
            val = ((r or {}).get("result", {}).get("result", {}).get("value") or 0)
            if val == last:
                if val >= 300 and time.time() - stable >= 2:
                    break
            else:
                last, stable = val, time.time()
            if val >= 15000:
                break
            time.sleep(0.7)
        out = cmd("Runtime.evaluate",
                  {"expression": "document.documentElement.outerHTML",
                   "returnByValue": True})
        html = ((out or {}).get("result", {}).get("result", {}).get("value") or "")
        try:
            ws.close()
        except Exception:
            pass
        try:
            requests.get(base + "/json/close/" + urllib.parse.quote(tgt.get("id", "")),
                         timeout=3)
        except Exception:
            pass
        return html, bool(html)
    except Exception as e:
        _log("Chrome 渲染失败 %s: %s", url, e)
        return "", False
