#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用自动化 Chrome(真实 profile 副本)打开一个页面并截图 / 读取页面结构。

只读工具:打开页面、截图、看 DOM 里有什么可点的。不做任何点击。
用法:
    python3 cdp_shot.py shot <url> <输出png>
    python3 cdp_shot.py tree <url>          # 列出页面上可点的元素
"""
import base64
import json
import sys
import time
from pathlib import Path

import capture
import requests
import websocket

PORT = 9222


def open_tab(url, wait=6):
    base = f"http://127.0.0.1:{PORT}"
    tgt = requests.put(base + "/json/new?about:blank", timeout=8).json()
    ws = websocket.create_connection(tgt["webSocketDebuggerUrl"], timeout=20)
    mid = [0]

    def cmd(method, params=None, timeout=30):
        mid[0] += 1
        i = mid[0]
        ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        ws.settimeout(timeout)
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == i:
                return m

    cmd("Page.enable")
    cmd("Runtime.enable")
    cmd("Page.navigate", {"url": url})
    time.sleep(wait)
    return ws, cmd, tgt


def shot(url, out, wait=6):
    ok, err = capture.ensure(PORT)
    if not ok:
        print("启动浏览器失败:", err)
        return 1
    ws, cmd, tgt = open_tab(url, wait)
    r = cmd("Page.captureScreenshot", {"format": "png"}, timeout=40)
    data = r.get("result", {}).get("data")
    if not data:
        print("截图失败:", json.dumps(r)[:200])
        return 1
    Path(out).write_bytes(base64.b64decode(data))
    title = cmd("Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True})
    t = title.get("result", {}).get("result", {}).get("value")
    loc = cmd("Runtime.evaluate",
              {"expression": "location.href", "returnByValue": True})
    u = loc.get("result", {}).get("result", {}).get("value")
    print("标题:", t)
    print("地址:", u)
    print("已保存:", out)
    ws.close()
    return 0


def tree(url, wait=6):
    ok, err = capture.ensure(PORT)
    if not ok:
        print("启动浏览器失败:", err)
        return 1
    ws, cmd, tgt = open_tab(url, wait)
    expr = """
    (() => {
      const out = [];
      const sel = 'a,button,[role=button],input,li,[class*=record],[class*=btn]';
      document.querySelectorAll(sel).forEach((e,i) => {
        const t = (e.innerText||e.value||e.placeholder||'').trim().slice(0,50);
        const r = e.getBoundingClientRect();
        if (!t && !e.placeholder) return;
        if (r.width === 0 || r.height === 0) return;
        out.push({tag:e.tagName, text:t, x:Math.round(r.x+r.width/2),
                  y:Math.round(r.y+r.height/2)});
      });
      return JSON.stringify(out.slice(0,120));
    })()
    """
    r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    v = r.get("result", {}).get("result", {}).get("value")
    try:
        items = json.loads(v)
    except Exception:
        print("读不到:", str(v)[:300])
        return 1
    print("页面上可交互元素 %d 个:" % len(items))
    for it in items:
        print("  (%4d,%4d) %-8s %s" % (it["x"], it["y"], it["tag"], it["text"]))
    ws.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "shot":
        sys.exit(shot(sys.argv[2], sys.argv[3]))
    if sys.argv[1] == "tree":
        sys.exit(tree(sys.argv[2]))
    print("未知命令")
