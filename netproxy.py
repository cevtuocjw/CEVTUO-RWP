# -*- coding: utf-8 -*-
"""出国线路 —— 让 app **走代理**,而不是裸连。

────────────────────────────────────────────────────────────────────────
为什么会有这个文件(2026-10-16,用户报「ft.com / economist 全都处理不了」)
────────────────────────────────────────────────────────────────────────
日志里是三种错,看起来像三个不同的问题:

    ConnectTimeoutError ... Connection to www.ft.com timed out. (connect timeout=25)
    SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]'))
    403 Client Error: Forbidden

前两种是**网络根本不通**;第三种是连上了、被 FT 的机器人/付费墙挡下来。
而在同一台机器上实测:

    直连 https://www.ft.com/...          25.0s 超时   code=000
    走 http://127.0.0.1:7890             1.15s        code=403   ← 通了

⇒ 有一半的失败**根本不是网站的问题,是没有走代理**。而为什么会没走:

  · **macOS 的系统代理是关着的**(scutil --proxy:HTTPEnable 0 / HTTPSEnable 0 /
    SOCKSEnable 0)—— Clash 在跑,但没设系统代理;
  · app 里的 **Python(requests)** 默认 trust_env,而 GUI app **继承不到**你在
    shell 里 export 的 HTTPS_PROXY ⇒ 裸连;
  · app 起的 **Chrome 读的是系统代理设置** ⇒ 系统没设 ⇒ 也裸连 ⇒ 浏览器错误页。

⇒ **两条腿同时断,而且断得一模一样**,于是看起来像「这个网站坏了」。
   (⚠️ 这又是一次「环境问题伪装成产品问题」——见 HANDOFF 里「静默」那一节。)

────────────────────────────────────────────────────────────────────────
做法:一处探测,写进**进程环境**
────────────────────────────────────────────────────────────────────────
探测出来的代理写进 `HTTP_PROXY / HTTPS_PROXY / NO_PROXY`,于是:

  · 所有 `requests` 调用**一行都不用改** —— 它默认 trust_env=True,自己会读;
  · Chrome 从同一个函数拿 `--proxy-server`。

⚠️ 三条必须守住的:
  1. **端口开着 ≠ 这是个能用的代理**。docker、各种 API 都爱占端口 ⇒
     每个候选都要**真的用它发一次请求**才认。
  2. **本地回环必须排除**(NO_PROXY)。否则 app 会拿代理去连自己的
     `127.0.0.1:8611` 和 Chrome 的 `127.0.0.1:9222` —— 那会**把整个界面弄没**。
  3. **探测不到就直连**,保持原来的行为 —— 没有代理的人不该因此变慢或被拦。
"""
import os
import socket
import subprocess
import sys
import time

# 显式要求「不要用代理」
OFF = {"off", "none", "0", "no", "direct", "直连", ""}

# 常见的本地代理端口。⚠️ 这是**启发式**,所以每一条都要**真的用它发一次请求**
# 才认 —— 见上面第 1 条。
CANDIDATE_PORTS = (7890, 7891, 7897, 1087, 10809, 8888, 1080, 8080, 6152)

# 探测目标:一个「不挂代理在国内通常打不开、挂了就能通」的小地址。
# 判据是「**它回没回 HTTP**」,不是「它回了什么」—— 我们测的是代理活不活,
# 不是这个网址能不能读。
PROBE_URL = "http://www.gstatic.com/generate_204"
PROBE_TIMEOUT = 4.0

_override = None          # 用户在设置页里填的(优先于自动探测)
_cache = None             # (时间戳, 代理url 或 None)
_TTL = 120.0


# ------------------------------------------------------------------ 小工具 ---
def _port_open(host, port, timeout=0.35):
    """只做一次 TCP 连接,超时压得很短 —— 探测用的,不能拖慢启动。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _norm(p):
    p = (p or "").strip()
    if not p:
        return None
    if p.lower() in OFF:
        return None
    if "://" not in p:
        p = "http://" + p
    return p


def works(proxy, timeout=PROBE_TIMEOUT):
    """真的用这个代理发一次请求。

    ⚠️ **任何 HTTP 响应都算它活着。** 我们要判的是「它是不是一个能用的转发代理」,
    不是「那个网址能不能打开」—— 后者是网站的事,不是线路的事。
    """
    proxy = _norm(proxy)
    if not proxy:
        return False
    try:
        import requests
        r = requests.get(PROBE_URL, proxies={"http": proxy, "https": proxy},
                         timeout=timeout)
        return r is not None and r.status_code > 0
    except Exception:
        return False


# ------------------------------------------------------------ 自动探测的几路 ---
def from_system():
    """读系统代理设置(macOS)。

    ⚠️ 用户这台机器实测是**关着的**,所以这条路通常什么都查不到 ——
    但它仍然要有:有人是真的把系统代理开起来的,那时候 app 应该跟着走。
    """
    if sys.platform != "darwin":
        return []
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return []
    d = {}
    for line in (out or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    out_urls = []
    if d.get("HTTPSEnable") == "1" and d.get("HTTPSProxy"):
        out_urls.append("http://%s:%s" % (d["HTTPSProxy"], d.get("HTTPSPort") or 80))
    if d.get("HTTPEnable") == "1" and d.get("HTTPProxy"):
        out_urls.append("http://%s:%s" % (d["HTTPProxy"], d.get("HTTPPort") or 80))
    if d.get("SOCKSEnable") == "1" and d.get("SOCKSProxy"):
        out_urls.append("socks5://%s:%s" % (d["SOCKSProxy"], d.get("SOCKSPort") or 1080))
    return out_urls


def from_env():
    """环境里已经有的(比如从终端启动、或者上一次 apply_env 写进去的)。"""
    out = []
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
              "HTTP_PROXY", "http_proxy"):
        p = _norm(os.environ.get(k))
        if p and p not in out:
            out.append(p)
    return out


def from_ports(host="127.0.0.1"):
    """按常见端口逐个探。**先判端口开没开**(几乎不花时间),
    再对开着的那个真的发一次请求 —— 顺序很重要,不然启动会卡几十秒。"""
    out = []
    for port in CANDIDATE_PORTS:
        if _port_open(host, port):
            out.append("http://%s:%d" % (host, port))
    return out


def _detect_uncached():
    """按可靠性排序:显式指定 > 环境 > 系统代理 > 常见端口。

    ⚠️ **每一个候选都要 works() 过关才用它。** 只判「有人告诉我有这个代理」
    是不够的 —— 一个填错的地址会让**所有**请求都失败,那比直连还糟。

    ⚠️⚠️ **空串 ≠ 关掉。** 空串是「**没设置**」,那时候要自动探测。
       我第一版就是在这里栽的:设置项的默认值是空串,于是每一次启动都被
       判成「用户要求直连」,**自动探测永远不执行**。症状极隐蔽 ——
       日志老老实实打着「出国线路:直连(没检测到可用代理)」,
       看起来像"这台机器上没有代理",而其实是我的判断写反了。
       只有 `off`/`none`/`0` 这类**明确写出来的**才算关掉。
    """
    v = "" if _override is None else str(_override).strip()
    if v:
        if v.lower() in OFF:
            return None                       # 用户明确要求「直连」
        # ⚠️ 只填了一个端口号("7890")也要当它—— 用户看占位符里那串地址,
        #    很自然就只记住端口。当成 "http://7890" 会解析失败、静默退回直连。
        if v.isdigit():
            v = "127.0.0.1:" + v
        p = v if "://" in v else "http://" + v
        return p if works(p) else None        # 填错了就退回直连,别把 app 弄死

    for p in from_env() + from_system() + from_ports():
        if works(p):
            return p
    return None


def detect(force=False):
    """返回可用的代理 url,没有就 None。结果缓存 2 分钟。"""
    global _cache
    now = time.time()
    if not force and _cache is not None and now - _cache[0] < _TTL:
        return _cache[1]
    p = _detect_uncached()
    _cache = (now, p)
    return p


# ------------------------------------------------------------------ 落地 ---
NO_PROXY = "127.0.0.1,localhost,::1,0.0.0.0"


def apply_env(force=False):
    """把探测结果写进**进程环境**。

    这是整个文件的关键一步:写完这一句,**所有 requests 调用一行都不用改**
    (它们默认 trust_env=True,自己去读),Chrome 也用同一份结果。
    """
    p = detect(force=force)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        if p:
            os.environ[k] = p
        else:
            os.environ.pop(k, None)
    # ⚠️⚠️ 回环**必须**排除。不排除的话,app 会拿代理去连自己的
    #     127.0.0.1:8611(界面)和 127.0.0.1:9222(Chrome 调试口)——
    #     那会把整个界面弄没,而且看起来像"代理把软件弄坏了"。
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = NO_PROXY
    return p


def set_override(value):
    """设置页里填的那个。None = 交给自动探测;空/off = 强制直连。"""
    global _override, _cache
    _override = value
    _cache = None


def status(force=False):
    """给界面看的。⚠️ 必须能分清三种状态,不能只回一个 bool:
        · 走代理(用了哪个)
        · 直连(没找到可用的)
        · 直连(你自己要求关掉的)
    后两种的界面文字必须不一样 —— 否则用户分不清"没有"和"被关了"。"""
    p = detect(force=force)
    v = "" if _override is None else str(_override).strip()
    if not v:
        mode = "auto"            # ⚠️ 空串 = 没设置 = 自动(见 _detect_uncached)
    elif v.lower() in OFF:
        mode = "off"             # 明确要求直连
    else:
        mode = "manual"
    return {"proxy": p, "mode": mode, "override": v,
            "no_proxy": NO_PROXY}


def chrome_flag():
    """给 Chrome 用的 `--proxy-server=` 值,没有就 None。

    ⚠️ Chrome 默认就绕过 loopback,所以**不用**再给 --proxy-bypass-list;
    给了 `<-loopback>` 反而会让它拿代理去连 127.0.0.1:9222(CDP),
    那会把自动化整条链路弄断。
    """
    p = detect()
    if not p:
        return None
    if p.startswith("socks5://"):
        return "socks5://" + p[len("socks5://"):]
    return p
