# -*- coding: utf-8 -*-
"""验收:用户报的那两条「处理不了」的链接,现在**真的能处理成章节**。

────────────────────────────────────────────────────────────────────────
用户原话(2026-10-16)
────────────────────────────────────────────────────────────────────────
  「为什么今天的 ft.com 的所有链接我都处理不了了…类似于这样,处理好」
  「the economist 网页也是都处理不了」

日志里失败长这样:
  ✗ https://www.ft.com/content/... —— 页面打不开(浏览器错误页) · Chrome;
    <HTTPSConnection(host='www.ft.com', port=443
服务端日志里对应三种错:ConnectTimeoutError / SSLEOFError / 403。

────────────────────────────────────────────────────────────────────────
为什么必须**跑真链接**,不能只测 netproxy
────────────────────────────────────────────────────────────────────────
netproxy 的单测只能证明「我们挑到了一条线路」。而这个 bug 有**两条腿**:
Python 抓取 和 Chrome 渲染 —— 两条都要走代理,而且要**同时**走,
少一条就仍然会是「页面打不开」。所以这里从**界面用的那个接口**打进去,
一直走到「章节里有几千字的正文」。

⚠️ 它会**真的往用户的库里写一个临时合集**,跑完删掉。这是这个项目里
   若干验证器一贯的做法(见 verify-ui-refresh.py)。失败也会删。
⚠️ 前置条件不满足(没代理 / app 没在跑)时**单列成 SKIP**,不算通过也不算失败。
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("RWP_BASE", "http://127.0.0.1:8611")
NAME = "__FT验收__"

# 用户日志里真实失败过的那几条
URLS = [
    "https://www.ft.com/content/da781749-f146-48a7-9b5c-83911addabae",
    "https://www.economist.com/science-and-technology/2026/10/12/"
    "ais-mathematics-blitz-pushes-science-into-strange-new-territory",
]
MIN_WORDS = 400          # 「付费墙预览」大概一百来字;完整正文几千字

OK, BAD, SKIP, REPORT = [], [], [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  — " + detail) if detail else ""))


def skip(name, why):
    SKIP.append(name)
    print("  ⏭ %s  — %s" % (name, why))


def report(name, value):
    """量出来但**不当闸门**的东西。和「通过」在输出上必须长得不一样 ——
    否则「N 项通过」会读成「全都验过了」,而其中一半只是"量了"。"""
    REPORT.append((name, value))
    print("  · %s  — %s" % (name, value))


def api(path, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    print("被测 app %s\n" % BASE)
    try:
        st = api("/api/state")
    except Exception as e:
        print("app 没在跑(%s)—— 跳过" % type(e).__name__)
        return 0

    # ① 线路:这是**前提**,不是结论
    net = api("/api/net/proxy?redetect=1")
    print("① 前提:出国线路")
    if not net.get("proxy"):
        skip("真实链接导入", "这台机器没探测到可用的代理 —— 没法验")
        print("\n══ 汇总: 0 通过 / 0 失败 / 1 跳过 ══")
        return 0
    check("走代理", True, net["proxy"])

    # ② Chrome 那条腿也要走代理 —— 只修 Python 一半是不够的
    #
    # ⚠️⚠️ 判据盯的是**浏览器进程自己的命令行**,不是日志里那句话。
    #     我第一版读的是 `/api/logtail?which=server`,结果报红 ——
    #     而**产品是对的**(Chrome 进程确实带着 --proxy-server)。
    #     它之所以红,是因为 `capture._log` 走的是 logging,而 logging
    #     当时**没有任何 handler** ⇒ 那句 INFO 从来没被写下来。
    #     ⇒ 又一次"探针的毛病伪装成被测对象的毛病"。
    #     现在这里读的就是那个唯一算数的东西:进程的 args。
    job = api("/api/capture/ensure", {})
    check("Chrome 起来了", bool(job.get("ok")), str(job.get("err") or ""))
    import subprocess
    px = ""
    try:
        pid = subprocess.run(
            ["lsof", "-nP", "-iTCP:9222", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=10).stdout.split()[0]
        cmd = subprocess.run(["ps", "-o", "command=", "-p", pid],
                             capture_output=True, text=True, timeout=10).stdout
        px = next((a.split("=", 1)[1] for a in cmd.split()
                   if a.startswith("--proxy-server=")), "")
    except Exception as e:
        px = "(%s)" % type(e).__name__
    check("★ 浏览器那条腿也走了代理(不是只修了 Python 一半)",
          px == net["proxy"], "浏览器进程的 --proxy-server=%r,期望 %r"
          % (px, net["proxy"]))

    # ③ 真的导一次
    cid = None
    try:
        c = api("/api/collection/create", {"title": NAME, "author": "cev"})
        cid = c.get("id")
        check("建好了临时合集", bool(cid), "cid=%s" % cid)
        r = api("/api/import/urls", {"collectionId": cid, "urls": URLS,
                                     "useChrome": True}, timeout=60)
        jid = r.get("job") or r.get("id") or r.get("jobId")
        check("导入任务起来了", bool(jid), "job=%s" % jid)

        art = []
        for _ in range(120):                     # 最多等 10 分钟
            time.sleep(5)
            try:
                j = api("/api/job?id=" + str(jid))
            except Exception:
                continue
            if j.get("status") in ("done", "error", "cancelled"):
                break
            art = api("/api/articles?collection=" + str(cid)).get("articles", [])
            if len(art) >= len(URLS):
                break
        art = api("/api/articles?collection=" + str(cid)).get("articles", [])
        print("      %d / %d 条进了库" % (len(art), len(URLS)))
        check("★ 两条链接都变成了章节(不再是「页面打不开」)",
              len(art) >= len(URLS), "库里 %d 条" % len(art))
        by = {a.get("url", ""): a for a in art}

        def one(urlpart):
            for u, a in by.items():
                if urlpart in u:
                    return a
            return {}

        ft = one("ft.com")
        ec = one("economist")
        ft_n = ft.get("body_len") or 0
        ec_n = ec.get("body_len") or 0
        print("      FT        %5s 字   note=%r" % (ft_n, (ft.get("note") or "")[:40]))
        print("      Economist %5s 字   note=%r" % (ec_n, (ec.get("note") or "")[:40]))

        # ---- 【必须】FT:这是用户报的那条,必须好 ----
        check("★ FT 拿到了**正文**(≥ %d 字,不是付费墙预览)" % MIN_WORDS,
              ft_n >= MIN_WORDS, "FT %s 字" % ft_n)
        check("★ FT 的 note 里**没有**「页面打不开 / 网络不通」",
              not any(k in (ft.get("note") or "")
                      for k in ("打不开", "超时", "ProxyError", "网络")),
              repr(ft.get("note") or ""))

        # ---- 【报告】Economist —— **不假装修好了** ----
        # 实测(2026-10-16):economist 在 Cloudflare 后面,走代理 curl 也回
        # 403 `Just a moment...`;app 的 Chrome 拿到的是"墙"页,BPC 自己在页面上
        # 写了 `BPC > no fix`。把 UA 换成 BPC 规则里指定的那条(Liskov),
        # **结果一模一样** ⇒ UA 不是那把钥匙,是 BPC 的规则跟不上了。
        # 所以这里判的不是"economist 能读了",而是**它的失败说得清不清楚**。
        if ec_n >= MIN_WORDS:
            report("Economist", "★ 拿到了 %d 字 —— 比预期好,记一笔" % ec_n)
            check("Economist 若成功,note 也不该是失败话术",
                  not any(k in (ec.get("note") or "")
                          for k in ("打不开", "超时", "ProxyError")))
        else:
            report("Economist", "只有 %s 字 ⇒ 付费墙没绕过去(见下面 note)" % ec_n)
            note = ec.get("note") or ""
            check("★ 绕不过去时,note 必须**说清是绕不过去**,"
                  "不能是「页面打不开 / 网络不通」这种误导话术",
                  ("绕不过去" in note) or ("no fix" in note) or ("登录" in note),
                  "note=%r" % note[:70])
            check("★ 也不能把「绕不过付费墙」和「网络不通」混为一谈",
                  not any(k in note for k in ("打不开", "超时", "ProxyError")),
                  "note=%r" % note[:70])
    finally:
        if cid:
            try:
                api("/api/collection/delete", {"id": cid})
                print("      (临时合集已删)")
            except Exception as e:
                print("      ⚠️ 临时合集没删掉:%s —— 请手动删 %s" % (e, NAME))

    print("\n══ 汇总: %d 通过 / %d 失败 / %d 跳过 / %d 报告 ══"
      % (len(OK), len(BAD), len(SKIP), len(REPORT)))
    for b in BAD:
        print("   ✗ " + b)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
