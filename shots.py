#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 CEVTUO-RWP2EPUB 的各个界面截下来,并合成一段演示 GIF。

用独立的 headless Chrome(不碰你日常浏览器,也不碰那个连着阿里云的自动化实例)。
界面是 SPA,靠页面里的 setView()/openArticle() 切换视图,所以每张图前先执行一段 JS。
"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333
URL = "http://127.0.0.1:8611/"
OUT = Path("/tmp/cevtuo_shots")
W, H = 1560, 1000


def launch():
    subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
        f"--window-size={W},{H}", "--force-device-scale-factor=2",
        "--no-first-run", "--no-default-browser-check",
        "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            requests.get(f"http://127.0.0.1:{PORT}/json/version", timeout=1.5)
            return True
        except Exception:
            time.sleep(0.5)
    return False


class T:
    def __init__(self):
        tgt = requests.put(f"http://127.0.0.1:{PORT}/json/new?{URL}",
                           timeout=10).json()
        self.ws = websocket.create_connection(tgt["webSocketDebuggerUrl"],
                                              timeout=30)
        self.i = 0
        self.cmd("Page.enable")
        self.cmd("Runtime.enable")

    def cmd(self, m, p=None, t=40):
        self.i += 1
        i = self.i
        self.ws.send(json.dumps({"id": i, "method": m, "params": p or {}}))
        self.ws.settimeout(t)
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == i:
                return r

    def ev(self, js):
        r = self.cmd("Runtime.evaluate", {"expression": js,
                                          "returnByValue": True})
        return (r.get("result", {}).get("result", {}) or {}).get("value")

    def shot(self, name, wait=1.6):
        time.sleep(wait)
        r = self.cmd("Page.captureScreenshot", {"format": "png"}, 60)
        d = r.get("result", {}).get("data")
        if not d:
            print("  截图失败:", name)
            return None
        p = OUT / f"{name}.png"
        p.write_bytes(base64.b64decode(d))
        print("  ✓", p.name, f"{p.stat().st_size//1024}KB")
        return p


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if not launch():
        print("headless Chrome 启动失败")
        return 1
    t = T()
    time.sleep(6)

    # 拿一个合集 id 和文章 id
    cid = t.ev("(async()=>{const r=await fetch('/api/collections').then(x=>x.json());"
               "return r.collections[0] && r.collections[0].id})()")
    t.cmd("Runtime.evaluate", {"expression": "1"})
    time.sleep(2)
    cid = t.ev("window.__cid || (async()=>{const r=await fetch('/api/collections')"
               ".then(x=>x.json()); return r.collections.length?r.collections[0].id:''})()")
    # 上面的 async 拿不到值,改用同步方式:直接问页面 store
    t.ev("(async()=>{const r=await fetch('/api/collections').then(x=>x.json());"
         "window.__cid=r.collections[0]?.id||'';})()")
    time.sleep(2.5)
    cid = t.ev("window.__cid")
    t.ev("(async()=>{const r=await fetch('/api/articles?collection='+window.__cid)"
         ".then(x=>x.json()); window.__aid=r.articles[0]?.id||'';})()")
    time.sleep(2.5)
    aid = t.ev("window.__aid")
    print("  合集 id:", cid, "| 文章 id:", aid)

    shots = [
        ("01-collections", "setView('collections')"),
        ("02-collection-detail", f"setView('collection','{cid}')"),
        ("03-import-dialog", f"setView('collection','{cid}'); setTimeout(()=>importModal('{cid}'),700)"),
        ("04-article-view", f"closeModal(); setTimeout(()=>openArticle('{aid}'),500)"),
        ("05-rss", "closeModal(); setView('rss')"),
        ("06-library", "setView('library')"),
        ("07-settings", "setView('settings')"),
    ]
    for name, js in shots:
        t.ev(js)
        t.shot(name, 2.2)

    # 顺便滚动 RSS 页拍两张,给 GIF 用
    t.ev("setView('rss')")
    time.sleep(2)
    for i, y in enumerate([0, 420, 900, 1500]):
        t.ev(f"window.scrollTo(0,{y})")
        t.shot(f"10-rss-scroll{i}", 0.9)

    t.ev("setView('collections')")
    time.sleep(1.5)
    t.shot("11-back", 1.0)
    print("\n素材目录:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
