# -*- coding: utf-8 -*-
"""验证 capture.goto() 会不会把「机器人校验中间页」当成成品正文收下来。

────────────────────────────────────────────────────────────────────────
这个 bug 是怎么来的(2026-10-10,用户报「有的链接导入后正文只有一百多字」)
────────────────────────────────────────────────────────────────────────
FT 会先发一张 "Security Verification / 请稍候…" 的中间页,十几秒后才换成真文章。
这张中间页最坑的地方是:

    · document.readyState === "complete"   (它真的加载完了)
    · innerText 恒为 554 字,**十几秒一个字都不变**   (在等后端校验)

而 goto() 的收尾判据是「**稳定 settle 秒 且 readyState 完成**」
—— 这两条中间页**全都满足**。于是它在 1.2 秒就把这张纸收下来了。
症状:章节标题「请稍候…」,正文「Financial Times Security Verification …」,
145 个字,而**全程没有任何报错**。

⇒ 判据测的不是它以为的东西。这正是 FACT 纪律 6/15 那个家族。

────────────────────────────────────────────────────────────────────────
为什么不直接拿 FT 来验
────────────────────────────────────────────────────────────────────────
试过了,**验不了**:FT 的校验是间歇的,清掉 cookie + localStorage
(`Storage.clearDataForOrigin`) 之后两组都直接拿到全文 —— 对照红不了,
看着像"这个检测没用",其实只是**它这次没发作**。
依赖被测方"按时发作"的对照,等于没对照。

所以这里**自己重放**那张中间页:机制是确定的,断言就也是确定的。
⚠️ 服务器一律用 ThreadingHTTPServer(单线程的会让并发抓取静默失败)。
"""
import http.server
import re
import socketserver
import sys
import threading
import time

import capture
import imports


# ---------------------------------------------------------------- 夹具 ------
# 忠实照抄 FT 那张中间页的可见文字(实测抓下来的原文)。
CHALLENGE_BODY = """
<h1>请稍候…</h1>
<div id="sec">
  <p>Financial Times</p>
  <p>Security Verification</p>
  <p>For help please visit help.ft.com . We apologise for any inconvenience.</p>
  <p>验证成功。正在等待 www.ft.com 响应</p>
  <p>Enable JavaScript and cookies to continue</p>
  <p>The following information can help our support team to resolve this issue.</p>
  <p>Error Code CG000 / 403</p>
  <p>Terms &amp; Conditions Privacy Policy Cookie Policy Manage Cookies</p>
  <p>&copy; THE FINANCIAL TIMES LTD 2026.</p>
</div>
"""

ARTICLE_BODY = "<h1>US 30-year Treasury yield hits highest since 2002</h1>" + \
               "".join("<p>%s</p>" % ("正文段落 " * 60) for _ in range(6))

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>%s</title></head><body>%s</body></html>"""


def challenge_page():
    return PAGE % ("请稍候…", CHALLENGE_BODY)


def swap_page():
    """先给中间页,12 秒后**在页面内**换成真文章 —— 复刻 FT 的实际行为。"""
    return """<!doctype html><html><head><meta charset="utf-8">
<title>请稍候…</title></head><body>%s
<script>
setTimeout(function () {
  document.title = "US 30-year Treasury yield hits highest since 2002";
  document.body.innerHTML = %r;
}, 12000);
</script></body></html>""" % (CHALLENGE_BODY, ARTICLE_BODY)


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/swap"):
            body = swap_page()
        else:
            body = challenge_page()
        b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  — " + detail) if detail else ""))


def arm(disable_detection, path, label):
    """返回 (正文可见字数, 耗时, why)。

    ⚠️⚠️ 这里踩过一次「量具自己坏了」:第一版 `n` 用的是**没剔 <script> 的**正则,
    而 /swap 那个夹具把整篇文章**放在 <script> 里**(等着 12 秒后塞进 body)
    —— 于是"可见字数"量出 207452,断言 `n < 600` 直接红,看着像产品坏了。
    同一次还踩了第二个:why 是在 `finally` **恢复检测之后**才求值的,
    于是"关掉检测"那一臂的 why 依然是"被挡住" —— 对照根本没关成。

    ⇒ 数内容一律 **先剔 script/style**,判据一律**在补丁还生效时**算完。
    """
    orig = capture._looks_interstitial
    if disable_detection:
        capture._looks_interstitial = lambda t: False      # ← 只关被测的那一侧
    t0 = time.time()
    try:
        html, err = capture.render(PORTAL + path, timeout=45)
        el = time.time() - t0
        n = len(re.sub(r"\s+", "", imports._text_only(html or "")))
        why = imports.browser_error_page(html or "")
    finally:
        capture._looks_interstitial = orig
    print("    %-16s %5.1fs  可见 %5d 字  err=%r  why=%r"
          % (label, el, n, err, why))
    return n, el, why


if __name__ == "__main__":
    srv = Server(("127.0.0.1", 0), H)
    PORTAL = "http://127.0.0.1:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("夹具服务器 %s (多线程)\n" % PORTAL)

    capture.kill_chrome()
    time.sleep(1.0)
    ok, err = capture.ensure()
    if not ok:
        print("Chrome 起不来:", err)
        sys.exit(2)

    # ── ① 负向对照:关掉检测,必须复现那个 bug ───────────────────────
    print("① 负向对照 —— 关掉拦截页检测(/swap,12 秒后换真文章)")
    n_off, t_off, why_off = arm(True, "/swap", "关掉检测")
    check("关掉检测时,确实把中间页当成品收了下来(这就是线上那个 bug)",
          n_off < 600, "只拿到 %d 字" % n_off)
    check("关掉检测时,收得很快 —— 说明它没等(1.2 秒 settle 就收)",
          t_off < 6.0, "%.1fs" % t_off)
    check("关掉检测时,中间页**没有**被认成错误页(所以它安安静静变成了一章)",
          why_off == "", "why=%r" % why_off)

    # ── ② 正向:开着检测,必须等过去并拿到真文章 ──────────────────
    print("\n② 开着拦截页检测(/swap,12 秒后换真文章)")
    n_on, t_on, why_on = arm(False, "/swap", "开着检测")
    # ⚠️ 判据要写成**契约**,不要写死数字。第一版写了 `n_on > 2000`,
    #    而夹具那篇文章本身只有 1524 字 —— 红的是阈值,不是产品。
    #    真正要证的是「拿到的是文章,不是那张纸」,所以跟中间页比。
    check("等过了校验,拿到的是**文章**,不再是那张中间页",
          n_on > n_off * 3, "文章 %d 字 vs 中间页 %d 字" % (n_on, n_off))
    check("拿到的是完整文章(不是只有个标题)",
          n_on > 1000, "拿到 %d 字" % n_on)
    check("确实等了 —— 耗时 > 中间页那 12 秒",
          t_on > 11.0, "%.1fs" % t_on)
    check("比关掉检测时明显更晚返回(证明这个分支真的在起作用)",
          t_on > t_off + 8.0, "%.1f vs %.1fs" % (t_on, t_off))

    # ── ③ 等不过去时,必须**说出来**,不能静默变成正文 ──────────────
    print("\n③ 检测开着,但中间页永远不消失(/challenge)")
    n_c, t_c, why_c = arm(False, "/challenge", "永远不放行")
    check("识别为「被机器人校验挡住」,而不是当成一章正文",
          "机器人校验" in why_c, "why=%r" % why_c)
    check("没有把它当成正常页面(why 非空)",
          why_c != "")

    print("\n══ 汇总: %d 通过 / %d 失败 ══" % (len(OK), len(BAD)))
    if BAD:
        for b in BAD:
            print("   ✗ " + b)
    srv.shutdown()
    sys.exit(1 if BAD else 0)
