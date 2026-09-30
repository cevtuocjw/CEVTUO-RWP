#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在**同一个标签页**上操作自动化 Chrome:点击、输入、求值、截图。

关键点:必须复用已有标签页。每次 /json/new 都会开新页,状态就丢了。
所以这里按 URL 关键字找到已存在的 target 再连上去。

点击坐标用**截图坐标**(截图是设备像素),内部会按 devicePixelRatio 换算成 CSS 像素。

用法:
    python3 cdp_act.py shot  <out.png>
    python3 cdp_act.py click <shot_x> <shot_y>
    python3 cdp_act.py type  <text>
    python3 cdp_act.py key   <Enter|Tab|Escape>
    python3 cdp_act.py eval  '<js>'
    python3 cdp_act.py open  <url>
    python3 cdp_act.py ratio            # 看截图/CSS 的换算比
"""
import base64
import json
import sys
import time
from pathlib import Path

import requests
import websocket

PORT = 9222
BASE = f"http://127.0.0.1:{PORT}"


class Tab:
    def __init__(self):
        self.ws = None
        self.mid = 0

    def _connect(self, url_hint=None):
        if self.ws:
            return
        targets = requests.get(BASE + "/json/list", timeout=8).json()
        page = None
        for t in targets:
            if t.get("type") != "page":
                continue
            if url_hint and url_hint not in (t.get("url") or ""):
                continue
            page = t
            break
        if page is None:
            for t in targets:
                if t.get("type") == "page":
                    page = t
                    break
        if page is None:
            page = requests.put(BASE + "/json/new?about:blank", timeout=8).json()
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"],
                                              timeout=25)
        self.url_hint = page.get("url", "")

    def cmd(self, method, params=None, timeout=30):
        self._connect()
        self.mid += 1
        i = self.mid
        self.ws.send(json.dumps({"id": i, "method": method,
                                 "params": params or {}}))
        self.ws.settimeout(timeout)
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == i:
                return m

    def evaluate(self, expr):
        r = self.cmd("Runtime.evaluate", {"expression": expr,
                                          "returnByValue": True})
        return (r.get("result", {}).get("result", {}) or {}).get("value")

    def ratio(self):
        """截图宽 / CSS 宽。截图是设备像素,点击要的是 CSS 像素。"""
        iw = self.evaluate("window.innerWidth") or 1280
        shot_w = self._shot_width()
        return (shot_w / iw) if shot_w else 1.0

    def _shot_width(self):
        r = self.cmd("Page.captureScreenshot", {"format": "png"})
        d = r.get("result", {}).get("data")
        if not d:
            return None
        from PIL import Image
        import io
        return Image.open(io.BytesIO(base64.b64decode(d))).size[0]

    def click(self, sx, sy):
        k = self.ratio()
        x, y = sx / k, sy / k
        for t in ("mousePressed", "mouseReleased"):
            self.cmd("Input.dispatchMouseEvent",
                     {"type": t, "x": x, "y": y, "button": "left",
                      "clickCount": 1})
            time.sleep(0.05)
        return x, y

    def type_text(self, s):
        self.cmd("Input.insertText", {"text": s})

    def key(self, name):
        codes = {"Enter": (13, "Enter"), "Tab": (9, "Tab"),
                 "Escape": (27, "Escape")}
        code, keyname = codes.get(name, (13, "Enter"))
        for t in ("keyDown", "keyUp"):
            self.cmd("Input.dispatchKeyEvent",
                     {"type": t, "windowsVirtualKeyCode": code,
                      "nativeVirtualKeyCode": code, "key": keyname,
                      "code": keyname, "text": "\r" if name == "Enter" else ""})
            time.sleep(0.05)

    def shot(self, out):
        r = self.cmd("Page.captureScreenshot", {"format": "png"}, timeout=45)
        d = r.get("result", {}).get("data")
        if not d:
            print("截图失败")
            return 1
        Path(out).write_bytes(base64.b64decode(d))
        print("已保存:", out, "| URL:", self.evaluate("location.href"))
        return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    # 默认附着到云解析控制台那个标签页
    hint = "console.aliyun.com"
    t = Tab()
    t.url_hint = hint
    try:
        t._connect(hint)
    except Exception as e:
        print("连接标签页失败:", e)
        return 1

    if cmd == "shot":
        return t.shot(sys.argv[2])
    if cmd == "ratio":
        print("换算比:", t.ratio(), "| innerWidth:",
              t.evaluate("window.innerWidth"))
        return 0
    if cmd == "click":
        x, y = t.click(float(sys.argv[2]), float(sys.argv[3]))
        print("已点击 CSS 坐标 (%.0f, %.0f)" % (x, y))
        return 0
    if cmd == "type":
        t.type_text(sys.argv[2])
        print("已输入:", sys.argv[2])
        return 0
    if cmd == "key":
        t.key(sys.argv[2])
        print("已按键:", sys.argv[2])
        return 0
    if cmd == "eval":
        print(t.evaluate(sys.argv[2]))
        return 0
    if cmd == "open":
        t.cmd("Page.navigate", {"url": sys.argv[2]})
        print("已导航:", sys.argv[2])
        return 0
    print("未知命令")
    return 1


if __name__ == "__main__":
    sys.exit(main())
