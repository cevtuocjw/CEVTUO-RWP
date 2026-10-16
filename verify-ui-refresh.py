# -*- coding: utf-8 -*-
"""验证:导入**中途把进度窗关掉**,章节列表最后依然会刷新。

────────────────────────────────────────────────────────────────────────
这个 bug 是怎么来的(用户原话:「你怎么导入完的内容在章节管理里面没有看到呢!!」)
────────────────────────────────────────────────────────────────────────
openJob() 里的轮询原来是这么收尾的:

    const host = $('#modalHost');
    if(!j || !j.id || !host.querySelector('.prog')){ clearInterval(timer); return; }

—— 用户嫌慢,把进度窗一关(点关闭 / 按 Esc),`.prog` 就没了,
于是轮询**当场停掉**,`onDone`(也就是 `loadChapters(cid)`)**永远不执行**。

后果特别阴:
  · 导入**其实成功了**,文章也真的写进库了;
  · 但界面上章节列表还是旧的,写着「还没有章节」;
  · **全程不报错**,进度条也确实跑完了。
⇒ 用户看到的是「导入完了在章节管理里看不到」。数据是对的,界面是死的。

修法:轮询和"画面还在不在"**必须解耦** —— 画面没了只跳过**更新画面**这一步,
收尾该跑还得跑。

────────────────────────────────────────────────────────────────────────
为什么要驱动真界面,而不是直接调函数
────────────────────────────────────────────────────────────────────────
这个 bug **整个都在前端**:接口、数据库、导入流程全是对的。
直接调 /api/import/urls 再看 /api/articles(我试过)会**全绿**,
因为它把"界面会不会刷新"这一步整个跳过了 —— 而那正是坏掉的地方。
"""
import json
import os
import sys
import threading
import time

import capture

# 夹具:每个页面慢 1.5 秒 —— 给我们留出"中途关掉进度窗"的窗口
SLOW = 1.5


def fixture():
    import http.server
    import socketserver

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(SLOW)
            b = ("<html><body><h1>Slow page</h1>" +
                 "".join("<p>%s</p>" % ("正文内容 " * 90) for _ in range(6)) +
                 "</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]



# ⚠️ 被测的 app 地址可以换 —— 这样一套断言能对着**沙盒实例**跑,
#    不必拿用户正在用的那份真实数据当靶子(它默认还是 8611)。
APP = os.environ.get("RWP_BASE", "http://127.0.0.1:8611")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  — " + detail) if detail else ""))


if __name__ == "__main__":
    srv, base = fixture()
    print("夹具 %s,每页慢 %.1fs\n" % (base, SLOW))

    # ⚠️ 先**复用**已经开着的那个 Chrome —— 无条件 kill_chrome() 会把用户
    #    正在用的采集浏览器杀掉再拉起(导入到一半会更糟),而我们只是要开一个标签页。
    try:
        tab = capture.Tab(capture.DEFAULT_PORT, timeout=60)
    except Exception:
        ok, err = capture.ensure()
        if not ok:
            print("Chrome 起不来:", err)
            sys.exit(2)
        tab = capture.Tab(capture.DEFAULT_PORT, timeout=60)
    try:
        tab.cmd("Page.navigate", {"url": APP + "/"})
        # 等 app 的脚本就绪
        for _ in range(60):
            time.sleep(0.5)
            if tab.evaluate("typeof renderCollection === 'function'"):
                break
        check("界面加载完了(renderCollection 可用)",
              bool(tab.evaluate("typeof renderCollection === 'function'")))

        # 建一个空合集并**进入它**
        cid = tab.evaluate("""(async () => {
            const r = await fetch('/api/collection/create', {method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({title:'__UI_测试__'})});
            return (await r.json()).id;
        })()""")
        # evaluate 对 Promise 不自动 await,用轮询拿
        for _ in range(20):
            cid = tab.evaluate("window.__tcid")
            if cid:
                break
            tab.evaluate("""(async () => {
                const r = await fetch('/api/collection/create', {method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({title:'__UI_测试__'})});
                window.__tcid = (await r.json()).id;
            })()""")
            time.sleep(0.5)
        check("建好了测试合集", bool(cid), "cid=%s" % cid)

        tab.evaluate("renderCollection(%s)" % json.dumps(cid))
        time.sleep(1.5)
        n0 = tab.evaluate("document.querySelector('#chCount').textContent")
        print("      进入合集后,章节计数 = %r" % n0)

        # ── 打开导入窗、粘两个网址、开始 ─────────────────────────────
        tab.evaluate("document.querySelector('#btnImport').click()")
        time.sleep(0.8)
        urls = [base + "/a", base + "/b"]
        tab.evaluate("window.__turls = %s" % json.dumps("\n".join(urls)))
        tab.evaluate("""(() => {
            const ta = document.querySelector('#uList');
            ta.value = window.__turls;
            ta.dispatchEvent(new Event('input', {bubbles:true}));
            document.querySelector('#uGo').click();
        })()""")
        time.sleep(1.2)
        has_prog = bool(tab.evaluate("!!document.querySelector('.prog')"))
        check("进度窗开出来了", has_prog)

        # ── ★ 关键动作:立刻把进度窗关掉 ────────────────────────────
        tab.evaluate("closeModal()")
        time.sleep(0.6)
        check("进度窗已经关掉了(复现用户的操作)",
              not tab.evaluate("!!document.querySelector('.prog')"))

        # ── 等任务在**后台**跑完 ───────────────────────────────────
        # 注意:evaluate 不会自动 await Promise,所以用"打桩 + 轮询"的写法。
        cid_js = json.dumps(cid)
        backend = 0
        for _ in range(60):
            time.sleep(1.0)
            tab.evaluate("""(async () => {
                const r = await fetch('/api/articles?collection=' + %s);
                window.__tcnt = (await r.json()).articles.length;
            })()""" % cid_js)
            time.sleep(0.3)
            backend = tab.evaluate("window.__tcnt || 0") or 0
            if backend >= len(urls):
                break
        check("后台确实把两条都导进去了(所以不是导入失败)",
              backend >= len(urls), "后端 %d 条" % backend)

        # ── ★ 真正被测的东西:界面自己刷新了吗 ──────────────────────
        # onDone 是在任务结束那一拍被调用的 —— 如果它会跑,这里就能看到。
        shown, rows = 0, 0
        for _ in range(20):
            time.sleep(1.0)
            txt = tab.evaluate("document.querySelector('#chCount').textContent") or ""
            shown = int("".join(c for c in txt if c.isdigit()) or 0)
            rows = tab.evaluate("document.querySelectorAll('#chList .ch').length") or 0
            if shown >= len(urls):
                break
        check("★ **关掉进度窗之后,章节列表依然自己刷新了**",
              shown >= len(urls),
              "界面显示 %d 章 / 后端 %d 条 / 期望 %d" % (shown, backend, len(urls)))
        check("界面上确实列出了章节行,不是「还没有章节」",
              rows >= len(urls), "实际渲染 %s 行" % rows)

        # 收尾
        tab.evaluate("""fetch('/api/collection/delete',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({id:%s})})""" % json.dumps(cid))
    finally:
        tab.close()
        srv.shutdown()

    print("\n══ 汇总: %d 通过 / %d 失败 ══" % (len(OK), len(BAD)))
    for b in BAD:
        print("   ✗ " + b)
    sys.exit(1 if BAD else 0)
