# -*- coding: utf-8 -*-
"""多层全文获取引擎(合法途径)。

模式栈(按可获得正文质量的优先级尝试,取最长且达标的):
  feed_full : RSS/Atom 本身已带全文(Substack 等)
  public    : 从文章公开页抓正文(trafilatura)
  cookie    : 用你自己的订阅 Cookie 会话(Netscape cookies.txt)抓正文
  amp_print : 站点公开提供的 AMP / 打印版页面
  ext       : 外接命令(你在 config 里指到自己的工具,如可导出全文的脚本)
  partial   : 只拿到 feed 摘要
  blocked   : 什么都拿不到

不包含任何绕过付费墙/伪装爬虫的"破解"逻辑。
"""
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

log = logging.getLogger("fulltext")

# 静音正文提取库的内部噪音日志(trafilatura/courlan/htmldate/justext)
for _noise_logger in ("trafilatura", "courlan", "htmldate", "justext",
                      "readability", "url_normalize"):
    logging.getLogger(_noise_logger).setLevel(logging.ERROR)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")

FULL_MODES = {"feed_full", "public", "cookie", "amp_print", "ext", "browser"}
MIN_FULL = 300          # 正文可信最短字符数
MIN_RATIO = 1.15        # 必须明显长于摘要才接受


# ------------------------------------------------------------- cookies ------
class CookieBox:
    """Netscape cookies.txt 加载 + 按域匹配。单例复用,线程安全(读只读)。"""

    def __init__(self, cfg):
        self._by_suffix = {}            # host suffix -> {name: value}
        self._warned = set()
        spec = (cfg or {}).get("cookies") or {}
        for suffix, path in spec.items():
            if not path or not os.path.exists(path):
                if suffix not in self._warned:
                    log.warning("Cookie 文件不存在: %s (%s)", suffix, path)
                    self._warned.add(suffix)
                continue
            try:
                self._by_suffix[suffix.lstrip(".").lower()] = \
                    self._load(Path(path))
            except Exception as e:
                log.warning("Cookie 文件读取失败 %s: %s", path, e)

    @staticmethod
    def _load(path):
        """支持两种格式:
        1) EditThisCookie 等导出的 JSON 数组 [{"domain","name","value",...},...]
        2) Netscape cookies.txt 文本。
        """
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if text.startswith("["):                     # JSON 数组
            try:
                import json
                arr = json.loads(text)
            except Exception:
                arr = []
            out = {}
            for c in arr:
                name = c.get("name")
                if not name:
                    continue
                val = c.get("value", "")
                # 会话 Cookie(session=true/无过期)也保留;有超时记录的忽略已过期的
                exp = c.get("expirationDate") or c.get("expires")
                if exp and exp < time.time() and c.get("session") is not True:
                    continue
                out[name] = val
            return out
        # Netscape 文本
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                parts = [p for p in line.split() if p]
            if len(parts) < 7:
                continue
            name, value = parts[5], parts[6]
            out[name] = value
        return out

    def for_url(self, url):
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not host:
            return None
        best, best_n = None, -1
        for suffix, jar in self._by_suffix.items():
            if host == suffix or host.endswith("." + suffix):
                if len(suffix) > best_n:
                    best, best_n = jar, len(suffix)
        return best


# --------------------------------------------------------------- fetching ----
def net_get(url, timeout, cookies=None, headers=None):
    import requests
    h = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, cookies=cookies, timeout=timeout,
                     allow_redirects=True)
    r.raise_for_status()
    return r


# ------------------------------------------------------------ feed 侧工具 -----
def fetch_feed(url, timeout, sess):
    import feedparser
    last = None
    for attempt in range(2):                       # 抓源失败重试一次,换 UA
        try:
            headers = _ALT_UAS[attempt] if attempt else {
                "User-Agent": UA,
                "Accept": "application/rss+xml,application/atom+xml,"
                          "application/xml,text/xml,*/*;q=0.8"}
            r = sess.get(url, headers=headers, timeout=timeout,
                         allow_redirects=True)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            if parsed.bozo and not parsed.entries:
                raise RuntimeError("RSS 解析失败")
            return parsed.entries
        except Exception as e:
            last = e
    raise RuntimeError(str(last))


def entry_link(entry):
    return (entry.get("link") or entry.get("id") or "").strip()


def entry_date_epoch(entry):
    import calendar
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        v = entry.get(key)
        if v:
            return calendar.timegm(v)
    return None


def _extract(html):
    from trafilatura import extract
    if not html:
        return None
    return extract(html, include_images=True, include_links=True,
                   include_formatting=True, output_format="html")


# -------------------------------------------------------------- providers ----
_ALT_UAS = [
    # 中文站、偏保守的站更常见默认值
    {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 Edg/122.0"),
     "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"},
    {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
     "Accept-Language": "*"},
]

_HOST_LK = threading.Lock()
_HOST_SEM = {}


def _host_guard(url):
    """按域名限流:同一域名最多 2 个并发请求,减少被反爬限流的概率。"""
    host = (urllib.parse.urlparse(url).hostname or "other").lower()
    with _HOST_LK:
        sem = _HOST_SEM.get(host)
        if sem is None:
            sem = threading.BoundedSemaphore(2)
            _HOST_SEM[host] = sem
    return sem


def _try_public(url, timeout, cookies):
    """公开页面正文。短正文/失败时换 UA 重试(同源限流)。"""
    with _host_guard(url):
        last = None
        for attempt in range(3):
            try:
                r = net_get(url, timeout, cookies, headers=_ALT_UAS[attempt])
                ctype = r.headers.get("Content-Type", "")
                if "html" not in ctype.lower() or len(r.content) > 15 * 1024 * 1024:
                    last = None
                    continue
                body = _extract(r.text)
                if body and _plain_len(body) >= MIN_FULL:
                    return body
                last = body
            except Exception as e:
                last = e
                log.debug("public 尝试%d失败 %s: %s", attempt + 1, url, e)
        return last if isinstance(last, str) else None


def _try_cookie(url, timeout, cookies):
    if not cookies:
        return None
    with _host_guard(url):
        try:
            r = net_get(url, timeout, cookies)
            if "html" not in r.headers.get("Content-Type", "").lower():
                return None
            return _extract(r.text)
        except Exception as e:
            log.debug("cookie 会话失败 %s: %s", url, e)
            return None


def _try_amp_print(url, timeout, cookies):
    """先找 <link rel=amphtml>,再抓 AMP 页正文。仅访问站点公开链接。"""
    with _host_guard(url):
        try:
            r = net_get(url, timeout, cookies)
            if len(r.content) > 15 * 1024 * 1024:
                return None
            m = re.search(rb'<link[^>]+rel=["\']amphtml["\'][^>]+href=["\']([^"\']+)',
                          r.content, re.I)
            if not m:
                m = re.search(rb'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']amphtml["\']',
                              r.content, re.I)
            if not m:
                return None
            amp = m.group(1).decode("utf-8", "ignore")
            amp = urllib.parse.urljoin(url, amp)
            ra = net_get(amp, timeout, cookies)
            if "html" not in ra.headers.get("Content-Type", "").lower():
                return None
            return _extract(ra.text)
        except Exception as e:
            log.debug("amp_print 失败 %s: %s", url, e)
            return None


def _browser_enabled(cfg):
    bc = (cfg or {}).get("browser") or {}
    return bool(bc.get("enabled"))


def _try_browser(url, timeout, cfg):
    """用独立 Chrome + 注入该域你的 Cookie 渲染取正文(不调用任何扩展逻辑)。"""
    try:
        import browser as _br
        cf = _br.cookie_file_for(cfg, url)
        if not cf:
            return None
        html, ok = _br.render_with_cookies(url, cf, cfg, timeout=min(timeout, 60))
        if not ok or not html:
            return None
        return _extract(html)
    except Exception as e:
        log.debug("browser 失败 %s: %s", url, e)
        return None


def _try_ext(url, cmd):
    """外接命令:stdout 返回 HTML/纯文本正文。cmd 是 argv 列表。"""
    if not cmd:
        return None
    try:
        p = subprocess.run([str(c) for c in cmd] + [url], capture_output=True,
                           timeout=120)
        if p.returncode != 0:
            return None
        out = p.stdout.decode("utf-8", "ignore").strip()
        return out if len(out) > MIN_FULL else None
    except Exception as e:
        log.warning("外接命令失败: %s", e)
        return None


# ---------------------------------------------------------------- engine -----
_BOX_CACHE = {}


def _cookie_box(cfg):
    """CookieBox 按 (suffix,path) 集合缓存,避免反复读盘/告警。"""
    spec = ((cfg or {}).get("cookies") or {}).items()
    key = tuple(sorted((str(s).lower(), str(p)) for s, p in spec))
    if key not in _BOX_CACHE:
        _BOX_CACHE[key] = CookieBox(cfg)
    return _BOX_CACHE[key]


def _plain_len(html):
    return len(re.sub(r"<[^>]+>", "", html or "").strip())


def get_body(url, summary_html, feed_has_full, cfg, timeout,
             no_public=False):
    """返回 (body_html, mode, detail)。body 为已清洗 HTML,可能仍是空。

    summary_html: feed 提供的原始 HTML(未清洗);feed_has_full: feed 本身带了全文。
    """
    box = _cookie_box(cfg)
    cookies = box.for_url(url)

    # 先用 RSS 自带内容:够长即视为全文
    summ_plain = _plain_len(summary_html or "")
    if feed_has_full and summ_plain >= MIN_FULL:
        return summary_html, "feed_full", "RSS 自带全文"

    ext_cfg = (cfg or {}).get("fulltext", {}).get("ext") or {}
    ext_cmd = ext_cfg.get("cmd") if ext_cfg.get("enabled") else None

    candidates = []                     # (mode, body, detail)
    if not no_public:
        b = _try_cookie(url, timeout, cookies)
        if b and _plain_len(b) > 0:
            candidates.append(("cookie", b, "订阅 Cookie 会话"))
        b = _try_public(url, timeout, cookies)
        if b and _plain_len(b) > 0:
            candidates.append(("public", b, "公开正文"))
        b = _try_amp_print(url, timeout, cookies)
        if b and _plain_len(b) > 0:
            candidates.append(("amp_print", b, "公开 AMP/打印版"))
        # 重武器:日常 Chrome 登录态渲染(默认只对配了 cookie 的域名,避免拖慢免费站)
        any_dom = bool((cfg or {}).get("browser", {}).get("any"))
        if _browser_enabled(cfg) and (cookies or any_dom):
            b = _try_browser(url, timeout, cfg)
            if b and _plain_len(b) > 0:
                candidates.append(("browser", b, "Chrome 登录态渲染"))
    b = _try_ext(url, ext_cmd)
    if b:
        candidates.append(("ext", b, "外接工具"))

    # 有摘要时兜底用摘要(partial);候选达标则取最长
    best = None
    for mode, body, detail in candidates:
        n = _plain_len(body)
        # 足够长,且比 RSS 摘要还长出 ≥150 字即视为"真全文"
        # (有些源摘要把全文几乎都塞进来了,不能用 15% 这种比例误拒)
        if n >= MIN_FULL and (summ_plain == 0 or n >= summ_plain + 150):
            if best is None or n > best[0]:
                best = (n, mode, body, detail)
    if best:
        _, mode, body, detail = best
        return body, mode, detail

    if summ_plain:
        return summary_html, "partial", "仅 RSS 摘要(未取得全文)"
    return "", "blocked", "未能取得内容"
