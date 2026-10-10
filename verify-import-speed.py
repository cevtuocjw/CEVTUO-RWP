# -*- coding: utf-8 -*-
"""验证「很多链接的时候特别慢」和「卡壳了没有任何办法」这两件事真的修好了。

用户原话:
  · 「为什么现在导入合集过程变得这么慢,很多链接的时候」
  · 「如果卡壳了我就没有任何办法」

两件事在代码里是同一个根:**串行 + 不可取消**。
  · import_urls  —— 已改成 ThreadPoolExecutor(见上一轮)
  · import_links —— **这一轮才改**,之前一个都没动(用户说的"很多链接"
    走的正是站点内链接那条路)

判据用**契约**,不用写死的秒数:
  · 并行 ⇒ 6 个各睡 2 秒的页面,总耗时应当**远小于** 6×2=12 秒
  · 可取消 ⇒ 点了取消之后,函数应当在**一个轮询周期**内返回(不是等当前跑完)

⚠️ 夹具服务器一律 ThreadingTCPServer —— 单线程的会把并行请求排成串行,
   于是「并行」那条断言会红,而红的是夹具不是产品(FACT 纪律 8)。
"""
import http.server
import socketserver
import sys
import threading
import time

import capture
import imports

DELAY = 2.0


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/never"):
            # 一个**永远加载不完**的页面 —— 就是用户说的「卡壳了」。
            # 发个头部然后一直挂着,Chrome 会一直转圈。
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>stuck</h1>")
            try:
                for _ in range(300):
                    time.sleep(1.0)
            except Exception:
                pass
            return
        time.sleep(DELAY)                       # 每个页面都慢
        body = ("<h1>Slow page %s</h1>" % self.path +
                "".join("<p>%s</p>" % ("正文内容 " * 80) for _ in range(5)))
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


class Ctx:
    """最小的 ctx 桩 —— 只实现导入流程真正用到的那几个方法。"""

    def __init__(self, cancel_at=None):
        self.lines = []
        self.t0 = time.time()
        self.cancel_at = cancel_at

    def log(self, m):
        self.lines.append(str(m))

    def set_total(self, n):
        pass

    def progress(self, i, n, m):
        pass

    def should_stop(self):
        return bool(self.cancel_at) and time.time() - self.t0 >= self.cancel_at


OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  — " + detail) if detail else ""))


if __name__ == "__main__":
    srv = Server(("127.0.0.1", 0), H)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    capture.kill_chrome()
    time.sleep(1.0)

    N = 6
    links = [{"href": "%s/slow%d" % (base, i), "title": "slow%d" % i}
             for i in range(N)]

    # ── ① 并行:总耗时必须远小于串行的 N×DELAY ─────────────────────────
    print("① import_links 是不是真的并行了(6 个页面,每个 %ss)" % DELAY)
    ctx = Ctx()
    t0 = time.time()
    # use_chrome=False ⇒ 走 requests 那条路,量的是"调度"而不是浏览器
    res = imports.import_links(ctx, 1, links, use_chrome=False, timeout=30,
                               workers=3)
    el = time.time() - t0
    serial = N * DELAY
    print("    %d 个页面用了 %.1fs(串行会是 %.1fs)" % (N, el, serial))
    check("全部导入成功", res["added"] == N, "added=%d" % res["added"])
    check("确实并行了 —— 比串行快得多",
          el < serial * 0.7, "%.1f < %.1f" % (el, serial * 0.7))

    # ── ② 可取消(HTTP 路径)────────────────────────────────────────
    # ⚠️ 这里的契约**只是「不再开新的」**,不是「把在途的掐断」——
    #    requests.get 发出去之后没法中断。所以 3 个在途页必须跑完。
    #    第一版把断言写成"一个页面耗时之内返回",结果 2.0s vs 2.0s 红了一条 ——
    #    红的是**断言写错了契约**,不是功能。真正会被卡住的在 ③。
    print("\n② 点「取消」之后不再开新页面(HTTP 路径,在途的收不回来)")
    ctx2 = Ctx(cancel_at=1.0)
    t0 = time.time()
    imports.import_links(ctx2, 1, links, use_chrome=False, timeout=30, workers=3)
    el2 = time.time() - t0
    print("    取消后 %.1fs 返回(3 个在途页仍要跑完)" % el2)
    check("没有把 6 个全跑完 —— 剩下的被跳过了",
          el2 < serial * 0.7, "%.1fs < %.1fs" % (el2, serial * 0.7))
    check("返回时间就是「在途那一批」的耗时,不多等",
          el2 < DELAY * 2, "%.1fs" % el2)

    # ── ③ 真的卡住的页面:Chrome 路径必须能中途逃出来 ─────────────────
    #    这才是用户说的「如果卡壳了我就没有任何办法」。
    #    页面永远加载不完 ⇒ 没有取消的话会一直等到 timeout(设成 45s)。
    print("\n③ 页面永远加载不完时,取消能不能逃出来(Chrome 路径)")
    ok, err = capture.ensure()
    if not ok:
        check("Chrome 起来了", False, str(err))
    else:
        stuck = [{"href": base + "/never", "title": "stuck"}]
        ctx3 = Ctx(cancel_at=2.5)
        t0 = time.time()
        imports.import_links(ctx3, 1, stuck, use_chrome=True, timeout=45,
                             workers=1)
        el3 = time.time() - t0
        print("    取消后 %.1fs 返回(timeout 设的是 45s)" % el3)
        check("卡住的页面没有把 45 秒超时耗完 —— 取消真的逃出来了",
              el3 < 12.0, "%.1fs" % el3)

    print("\n══ 汇总: %d 通过 / %d 失败 ══" % (len(OK), len(BAD)))
    for b in BAD:
        print("   ✗ " + b)
    srv.shutdown()
    sys.exit(1 if BAD else 0)
