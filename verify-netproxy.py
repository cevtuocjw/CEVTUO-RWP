# -*- coding: utf-8 -*-
"""验证:出国线路(netproxy.py)—— 用户报的「ft.com / economist 全都处理不了」。

────────────────────────────────────────────────────────────────────────
这个 bug 是什么
────────────────────────────────────────────────────────────────────────
日志里三种错:ConnectTimeoutError / SSLEOFError / 403。前两种是**网络根本不通**。
实测同一台机器:
    直连 https://www.ft.com/...       25.0s 超时   code=000
    走 http://127.0.0.1:7890          1.15s        code=403   ← 通了
而 macOS 的**系统代理是关着的**(scutil:HTTPEnable 0 / HTTPSEnable 0),所以
app 里的 Python 和 app 起的 Chrome **两条腿同时在裸连**,而且断得一模一样
⇒ 看起来像「这个网站坏了」。

────────────────────────────────────────────────────────────────────────
三类断言,分开计数(⚠️ 未达标不能混进「通过」)
────────────────────────────────────────────────────────────────────────
  【必须】不依赖网络和这台机器的状态 —— 逻辑、回环、环境变量落地
  【报告】依赖这台机器(有没有代理、代理通不通),量出来并写清
  【跳过】前置条件不满足时**单列**,不算通过也不算失败

跑负向对照:  NEGCTL=1 ./.venv/bin/python verify-netproxy.py
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netproxy                                            # noqa: E402

OK, BAD, SKIP, REPORT = [], [], [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  — " + detail) if detail else ""))


def skip(name, why):
    SKIP.append(name)
    print("  ⏭ %s  — %s" % (name, why))


def report(name, value):
    REPORT.append((name, value))
    print("  · %s  — %s" % (name, value))


# --------------------------------------------------------------- 【必须】 ---
print("① 线路判定(不依赖网络)")

# ⚠️ 打桩:_detect_uncached 里"这条路能不能用"要发真实请求,离线跑不了。
#    这里只替换"能不能用"那一个判断,**判定逻辑本身是真的**。
_real_works = netproxy.works
netproxy.works = lambda p, timeout=None: True          # 【打桩】
try:
    netproxy.set_override(None)
    check("【必须】没设置过 + 探测有结果 ⇒ 走代理",
          netproxy.detect(force=True) == "http://127.0.0.1:7890",
          str(netproxy.detect(force=True)))

    # ⚠️⚠️ 这条是这一版真正的 bug:设置项的**默认值是空串**,而我把空串
    #      也当成了「用户要求直连」⇒ 自动探测永远不执行,而日志老老实实
    #      打着「直连(没检测到可用代理)」—— 看起来像"这台机器上没有代理"。
    netproxy.set_override("")
    check("【必须】★ 空串 = **没设置**(不是关掉)⇒ 必须仍然自动探测",
          netproxy.detect(force=True) == "http://127.0.0.1:7890",
          "detect=%r" % (netproxy.detect(force=True),))
    check("【必须】空串时 mode 必须是 auto 而不是 off",
          netproxy.status(force=True)["mode"] == "auto")

    netproxy.set_override("off")
    check("【必须】'off' = 用户明确要求直连", netproxy.detect(force=True) is None)
    check("【必须】'off' 时 mode == off(界面要分得清'没有'和'被关了')",
          netproxy.status(force=True)["mode"] == "off")

    netproxy.set_override("7890")
    check("【必须】只填一个端口号也认(用户很自然只记端口)",
          netproxy.detect(force=True) == "http://127.0.0.1:7890",
          str(netproxy.detect(force=True)))
finally:
    netproxy.works = _real_works

# 填错地址 ⇒ **退回直连**,不能把整个 app 弄死
netproxy.set_override("http://127.0.0.1:9")
check("【必须】填了个连不上的地址 ⇒ 退回直连(而不是把 app 拖死)",
      netproxy.detect(force=True) is None)
netproxy.set_override(None)

print("\n② 写进进程环境(requests 全靠这一步)")
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("https_proxy", None)
p = netproxy.apply_env(force=True)
report("这台机器探测到的线路", p or "(没有 ⇒ 直连)")
if p:
    check("【必须】apply_env 把 HTTPS_PROXY 写进了 os.environ",
          os.environ.get("HTTPS_PROXY") == p, os.environ.get("HTTPS_PROXY", "(没设)"))
else:
    skip("apply_env 写进 os.environ", "这台机器没探测到代理")

# ⚠️⚠️ 回环必须排除。不排除的话 app 会拿代理去连自己的 127.0.0.1:8611(界面)
#      和 127.0.0.1:9222(Chrome 调试口)—— 那会把整个界面弄没。
np = (os.environ.get("NO_PROXY") or "")
check("【必须】★ NO_PROXY 必须覆盖回环(否则界面会整个打不开)",
      "127.0.0.1" in np and "localhost" in np, "NO_PROXY=%r" % np)

# 真的验一次:回环请求**不能**被代理吃掉
if p:
    try:
        import requests
        r = requests.get("http://127.0.0.1:8611/api/state", timeout=5)
        check("【必须】★ 真发一次回环请求,没被代理吃掉(界面还活着)",
              r.status_code == 200, "code=%s" % r.status_code)
    except Exception as e:
        skip("回环请求实测", "app 没在 8611 上跑(%s)" % type(e).__name__)
else:
    skip("回环请求实测", "这台机器没代理,谈不上被吃掉")

# --------------------------------------------------------------- 【报告】 ---
print("\n③ 这台机器上线路到底通不通(报告)")
if p:
    try:
        import requests
        r = requests.get("https://www.ft.com/", timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        report("走代理打 ft.com", "code=%s" % r.status_code)
        check("【必须】走代理时 ft.com 至少**连得上**(403 也算连上 —— 那是被网站挡,不是网络不通)",
              r.status_code > 0, "code=%s" % r.status_code)
    except Exception as e:
        report("走代理打 ft.com", "✗ %s" % type(e).__name__)
        check("【必须】走代理时 ft.com 至少连得上", False, str(e)[:80])
    # 直连做对照 —— 证明"走代理"这个动作**真的改变了结果**
    try:
        import requests
        s = requests.Session()
        s.trust_env = False                       # 绕开我们刚写进去的代理变量
        s.proxies = {}
        r = s.get("https://www.ft.com/", timeout=8,
                  headers={"User-Agent": "Mozilla/5.0"})
        report("直连打 ft.com", "code=%s" % r.status_code)
        report("⇒ 结论", "这台机器**直连也能通**,说明代理不是必需的"
               if r.status_code > 0 else "这台机器**直连不通**、必须走代理 ⇒ 正是用户那个 bug 的场景")
    except Exception as e:
        report("直连打 ft.com", "✗ %s(%s)" % (type(e).__name__, str(e)[:60]))
        report("⇒ 结论", "★ 直连不通、走代理能通 ⇒ 代理这条路**真的修好了用户那个 bug**")
else:
    skip("线路实测", "没探测到代理")

# Chrome 用的旗标要和 Python 用的一致 —— 两边不一致就会"一条通一条不通"
try:
    import netproxy as _np
    cf = _np.chrome_flag()
    if p:
        check("【必须】Chrome 的 --proxy-server 和 Python 用的是同一个线路",
              cf == p, "chrome=%r python=%r" % (cf, p))
    else:
        check("【必须】没线路时 Chrome 也要直连(不能给个空旗标)",
              cf is None, "chrome_flag=%r" % (cf,))
except Exception as e:
    check("Chrome 旗标", False, str(e))

if "--negctl" in sys.argv or os.environ.get("NEGCTL") == "1":
    print("\n(负向对照请跑 negctl-netproxy.py)")

print("\n══ 汇总: %d 通过 / %d 失败 / %d 跳过 / %d 报告 ══"
      % (len(OK), len(BAD), len(SKIP), len(REPORT)))
for b in BAD:
    print("   ✗ " + b)
sys.exit(1 if BAD else 0)
