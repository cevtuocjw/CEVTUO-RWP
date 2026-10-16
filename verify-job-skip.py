# -*- coding: utf-8 -*-
"""验证「跳过这一章」—— 用户原话:

    「如果有遇到卡壳的我可以中途跳过(多链接时候),我有跳过这一章继续的按键,
      确保我可以一定做完,而不是只能取消」

所以这里要证的**不是**"有个按钮",而是三条契约:

  ① 跳过之后整批**还能跑完**(不是变成取消)
  ② 只有**卡住的那一条**被丢掉,其它链接照常导入
  ③ 卡住那条**不会把超时耗完**才交差(它就是"卡壳"本壳)

⚠️ 夹具服务器一律 ThreadingHTTPServer —— 单线程的会把并发请求排成串行,
   "别的照常导入"那条会假红,而红的是夹具不是产品。

⚠️ 这里走的是**app 的真实 HTTP 接口**(127.0.0.1:8611),不是直接调函数。
   直接调函数会漏掉一层:跳过的请求是怎么从界面走到后台线程的。
"""
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.request

APP = os.environ.get("RWP_BASE", "http://127.0.0.1:8611")
TIMEOUT_S = 45          # 故意设得比夹具的"卡死时间"短很多,好用时间当判据


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/never"):
            # 永远加载不完 —— 就是用户说的「卡壳」。
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>stuck</h1>")
            for _ in range(400):
                time.sleep(1.0)
            return
        body = ("<html><body><h1>OK %s</h1>" % self.path +
                "".join("<p>%s</p>" % ("正文内容 " * 90) for _ in range(6)) +
                "</body></html>")
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


def api(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(APP + path, data=data,
                                 method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


if __name__ == "__main__":
    srv = Server(("127.0.0.1", 0), H)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        api("/api/collections")            # 随便一个真实存在的 GET 接口
    except Exception as e:
        print("app 没在跑(127.0.0.1:8611):", e)
        sys.exit(2)

    col = api("/api/collection/create", {"title": "__跳过测试__"})
    cid = col.get("id")
    print("合集 %s  夹具 %s\n" % (cid, base))

    urls = [base + "/never"] + [base + "/ok%d" % i for i in range(1, 4)]
    jid = api("/api/import/urls",
              {"collectionId": cid, "urls": urls, "useChrome": True})["job"]
    print("任务 %s,共 %d 个网址(第 1 个永远加载不完),超时设 %ds"
          % (jid, len(urls), TIMEOUT_S))

    # ── ① 等**确认有东西在跑**了再点跳过 ────────────────────────────
    # ⚠️ 不能用 `time.sleep(3)` 等固定秒数:启动 Chrome 那一段要 1~5 秒,
    #    没起完的时候 alive=0、跳过必然返回 None —— 那不是功能坏了,
    #    是**测试等错了东西**(FACT:等条件,不要等固定秒数)。
    mid, t_wait = None, time.time()
    while time.time() - t_wait < 40:
        mid = api("/api/job?id=" + jid)
        if mid.get("status") not in ("running", "queued"):
            break
        if (mid.get("alive") or 0) > 0:
            break
        time.sleep(0.5)
    print("      等 %.1fs 后 alive=%s done=%s/%s"
          % (time.time() - t_wait, mid.get("alive"),
             mid.get("done"), mid.get("total")))
    check("等到了「有链接正在抓」这个条件", (mid.get("alive") or 0) > 0,
          "alive=%s" % mid.get("alive"))

    t0 = time.time()
    sk = api("/api/job/skip", {"id": jid})
    check("跳过接口接受了请求", bool(sk.get("ok")), "resp=%s" % sk)
    skipped_i = sk.get("skipped")
    print("      (跳过的是索引 %s)" % skipped_i)
    # ⚠️⚠️ **这条才是关键**:跳过的必须是**第 1 条(卡住的那个,索引 0)**。
    #     第一版实现挑的是"最近开始的"那个,于是跳掉了一个正常的链接,
    #     而接口照样回 ok:true、日志照样写「已跳过」—— 只看"有没有跳过"
    #     是看不出来的。判据必须落在**跳过的到底是哪一条**上。
    check("★ 跳掉的是**卡住的那一条**(索引 0),不是随便一条正常的",
          skipped_i == 0, "skipped=%s" % skipped_i)

    # ── ② 等它跑完 ────────────────────────────────────────────────
    j = None
    for _ in range(60):
        time.sleep(1.0)
        j = api("/api/job?id=" + jid)
        if j.get("status") not in ("running", "queued"):
            break
    el = time.time() - t0
    print("      跳过之后 %.1fs 结束,status=%s" % (el, j.get("status")))

    check("★ 跳过后整批**跑完了**,没有变成取消",
          j.get("status") == "done",
          "status=%s message=%r" % (j.get("status"), j.get("message")))
    check("★ 卡住那条没有把 %ds 超时耗完" % TIMEOUT_S,
          el < TIMEOUT_S * 0.6, "%.1fs" % el)

    res = j.get("result") or {}
    arts = api("/api/articles?collection=" + cid)["articles"]
    notes = [a.get("note") or "" for a in arts]
    print("\n      章节 %d 条:" % len(arts))
    for a in arts:
        print("        · %-28s %s" % ((a.get("title") or "")[:28],
                                      (a.get("note") or "")[:40]))

    # ── ③ 只有卡住那条被丢,其它照常导入 ─────────────────────────────
    check("★ 三条正常的**都导进来了**",
          res.get("added", 0) >= 3, "added=%s" % res.get("added"))
    check("★ 卡住的那条被标成「已跳过」,而且**和失败分开写**",
          any("已跳过" in n for n in notes),
          "notes=%s" % [n[:20] for n in notes])
    check("被跳过的章节**仍然建了占位**（不是凭空少一章）",
          len(arts) == len(urls),
          "章节 %d 条 / 网址 %d 条" % (len(arts), len(urls)))

    # 收尾
    api("/api/collection/delete", {"id": cid})
    srv.shutdown()
    print("\n══ 汇总: %d 通过 / %d 失败 ══" % (len(OK), len(BAD)))
    for b in BAD:
        print("   ✗ " + b)
    sys.exit(1 if BAD else 0)
