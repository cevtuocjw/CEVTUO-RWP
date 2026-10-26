# -*- coding: utf-8 -*-
"""导入流水线:多个网址 / 网站内链接 / RSS / Markdown → 合集章节。

取正文默认走**真实 Chrome profile 副本渲染**(capture.render),
所以 archive.today、以及靠 Bypass Paywalls Clean 才解锁的页面都能拿到全文;
Chrome 不可用时自动退回普通 HTTP 抓取,并在结果里标注来源。
"""
import logging
import re
import threading

import capture
import extract_page
import sanitize
import store

log = logging.getLogger("imports")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")

# 明显不是文章正文的页面标记
_JUNK = re.compile(
    r"(enable javascript|verify you are human|just a moment|"
    r"checking your browser|cf-browser-verification|"
    r"please turn javascript on)", re.I)

# 付费墙绕不过去时,**Bypass Paywalls Clean 自己会往页面里插一句**
#   <div id="bpc_nofix">BPC > no fix …</div>
# ⚠️ 这句话比我们能猜的任何东西都准 —— 它是**扩展自己下的结论**。
#    2026-10-16 查 economist 时就是靠它在几分钟内定案的:页面正文字数 38,
#    而整个 body 就是这一句。没有它,我们只能笼统地报
#    「页面需要登录,或正文无法解析」—— 那句话把三种完全不同的情况
#    (要登录 / 扩展绕不过 / 页面结构不认识)混成一句,用户没法据此做任何事。
# ⚠️ 查的是**整篇** html,不是前 8000 字:`bpc_nofix` 是插在正文位置上的,
#    付费墙页面往往前面塞了几百 KB 的脚本,前 8000 字里根本看不到它。
_BPC_NOFIX = re.compile(r'id=["\']bpc_nofix["\']', re.I)


_MD_LINK = re.compile(r"\[[^\]]*\]\(\s*(https?://[^\s)]+)\s*\)")
_ANGLE = re.compile(r"<(https?://[^>\s]+)>")
# ⚠️ 字符类里必须排除**中文标点**。不排的话
#    `https://a.com/1，下一篇：https://b.com/2` 会被当成**一个**网址吞下去。
_BARE = re.compile(r"https?://[^\s<>\"'()\[\]{},;，。、；：！？（）【】《》"
                   r"“”‘’…]+")


def normalize_urls(text_or_list):
    """把用户粘进来的东西解析成干净的 URL 列表。

    ⚠️ 用户从浏览器/笔记里复制出来的经常**不是裸网址**,而是 Markdown 链接
    `[标题](https://…)`。早期直接 `strip()` 就当网址用,于是
    requests 抛 `No connection adapters were found for '[标题](https://…)'`
    —— 界面上显示的是「抓取失败」,看起来像网络问题,其实是**格式没解析**。

    支持的格式(一行里混着好几个也行):
      · 裸网址          https://example.com/a
      · Markdown 链接   [标题](https://example.com/a)
      · 尖括号          <https://example.com/a>
      · 标题 + 网址     Some title https://example.com/a
      · 后面带中文标点  https://example.com/a，下一篇：…
    """
    items = [text_or_list] if isinstance(text_or_list, str) else list(text_or_list)
    out, seen = [], set()
    for raw in items:
        if not raw:
            continue
        s = str(raw).strip()
        if not s:
            continue
        # ⚠️ 三遍**都要跑**,不能「找到 Markdown 链接就跳过裸网址」
        #    —— 一行里常常 Markdown 和裸网址混着,跳过的那些就静默丢了。
        found = _MD_LINK.findall(s) + _ANGLE.findall(s) + _BARE.findall(s)
        for u in found:
            u = u.rstrip(".,;:!?)]}，。；：！？）】》\"'")
            if u and u not in seen:
                seen.add(u)
                out.append(u)
    return out


def http_get(url, timeout=25):
    import requests
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout,
                     allow_redirects=True)
    r.raise_for_status()
    return r.text


# --------------------------------------------------------------- 阅读器代理 ---
# ⚠️⚠️ 为什么需要这一层(2026-10-26 用户报「new york times 要确保好用」):
#     NYT 前面是 DataDome。它**不看 UA、不看 headless**——同一台机器上
#     真 Chrome、非 headless、换 5 个代理节点,拿到的都是同一张 771 字节的
#     「Please enable JS and disable any ad blocker」。
#     判定发生在**我们的出口 IP**上,所以本机怎么改浏览器参数都没用。
#
#     而 Yandex 的网页翻译代理是**它自己的服务器去抓**,出口 IP 不同,
#     实测同一篇 NYT 能拿到 13505 字正文(标题、正文都对得上)。
#     ⇒ 只把它当**兜底**:正常抓取拿到的正文够长时,永远不走这条。
#     站点是公开网页,不涉及登录态;但它确实经过第三方,所以日志里要写明。
#
#     只用于「本机被反爬挡住」的情况,不是通用提取器 —— 对 FT/Economist
#     这种**付费墙**(不是反爬)无效,它们仍然靠 Bypass Paywalls Clean。
READER_URL = "https://translate.yandex.com/translate?url={u}&lang={lang}"


def reader_get(url, timeout=40, lang="en"):
    """通过网页翻译代理取整页 HTML。失败返回 ("", 原因)。"""
    import requests
    from urllib.parse import quote
    if not re.match(r"^https?://", str(url or ""), re.I):
        return "", "不是 http(s) 链接"
    try:
        r = requests.get(READER_URL.format(u=quote(url, safe=""), lang=lang),
                         headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        if "html" not in (r.headers.get("Content-Type") or "").lower():
            return "", "阅读器没有返回网页"
        return r.text, ""
    except Exception as e:
        log.warning("阅读器代理失败 %s: %s", url, e)
        return "", _short_err(e)


# 浏览器错误页的措辞 —— 就是用户在地址栏里看到的那几句话。
_ERRTEXT = re.compile(
    r"(检查您的互联网连接|请检查所有网线|意外终止了连接|连接已重置|"
    r"无法访问此网站|网站无法访问|响应时间过长|"
    r"check your internet connection|check your network cables|"
    r"this site can'?t be reached|took too long to respond|"
    r"connection (?:was )?reset|dns_probe_finished)", re.I)


def browser_error_page(html):
    """认出浏览器自己的错误页,返回原因;不是错误页返回 ""。

    ⚠️ 为什么必须单独认:这类页面是**一次「成功的抓取」**——
    HTTP 200、HTML 结构完整、长度也够,所以状态码和长度都拦不住它。
    它会安安静静地变成一章正文,正文写着「请检查您的互联网连接是否正常」。
    用户拿到的是一本「有正文、但正文是报错」的书,而且没有任何提示。
    """
    if not html:
        return ""
    low = html[:30000].lower()
    # ① 结构特征 —— 浏览器自己生成的错误页有固定 DOM,这一条最可靠,不看长度。
    if "main-frame-error" in low or 'class="neterror"' in low or "error-code" in low:
        m = re.search(r"ERR_[A-Z_]{3,}", html[:30000])
        return f"页面打不开({m.group(0) if m else '浏览器错误页'})"
    # ② ③ 文字特征 —— 用「正文有多少字」判断,不能用「页面多少字节」。
    #    ⚠️ 我一开始按页面体积判,20KB 以下就算错误页。实测站不住:
    #    纯文字的文章页只有 5KB,讲 Chrome 报错的文章正文明明写着 ERR_CACHE_MISS。
    #    错误页的真正特征是**几乎没正文**(实测几十到几百字),正常文章是几千字。
    if _page_text_len(html) > 2000:
        return ""
    m = re.search(r"\b(ERR_[A-Z_]{3,})\b", html)
    if m:
        return f"页面打不开({m.group(1)})"
    if _ERRTEXT.search(html[:6000]):
        return "页面打不开(网络连接被中断)"
    # ④ 机器人校验中间页 —— 等够了还没放行,就老实说出来。
    #    ⚠️⚠️ 不认它会怎样:它 500 多字、正文完整、HTTP 200,
    #    于是**安安静静地变成一章正文**,标题「请稍候…」,正文
    #    「Financial Times Security Verification …」。
    #    用户拿到一本「有正文,但正文是校验页」的书,而且没有任何提示。
    #    (这就是 2026-10-10「FT 只抓到 145 字」的另一半原因。)
    if capture._looks_interstitial(_text_only(html)):
        return "被网站的机器人校验挡住(等过 30 秒仍未放行,稍后重试)"
    return ""


def _text_only(html):
    """把 HTML 压成可见文字 —— 只为在拦截页判据里用。"""
    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"[ \t\r\f\v]+", " ", s)


def _page_text_len(html):
    """粗略数一下可见文字有多少 —— 只用来判断「这页是不是几乎没内容」。"""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html[:200000])
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return len(re.sub(r"\s+", "", s))


def _short_err(e):
    """把 requests 那种三层嵌套的异常压成一句人话。"""
    s = str(e)
    m = re.search(r"(?:Caused by )?\w*Error\((.+?)\)", s)
    if m:
        s = m.group(1)
    s = s.strip().strip("'\"")
    return (s[:150] + "…") if len(s) > 150 else s


def fetch_html(url, use_chrome=True, timeout=60, should_stop=None,
               should_skip=None):
    """返回 (html, via, err)。via ∈ chrome | http | ''(失败)。

    ⚠️⚠️ **取消和跳过都必须穿过去**:`capture.Cancelled` / `capture.Skipped`
    不能被下面这几个 `except Exception` 吃掉。吃掉会发生什么(实测 2026-10-10):
    Chrome 已经在 0.5 秒内响应了取消并抛出,这里却把它记成"Chrome 渲染失败",
    然后**转身去试直连** —— 而直连没有中断机制,一个滴流的页面又能挂 25 秒。
    用户点了取消,整批反而多等 27.6 秒。**取消被降级 = 取消失效。**
    """
    errs = []
    if use_chrome:
        try:
            html, err = capture.render(url, timeout=timeout,
                                       should_stop=should_stop,
                                       should_skip=should_skip)
            if html and len(html) > 400:
                bad = browser_error_page(html)
                if not bad:
                    return html, "chrome", ""
                errs.append(bad + " · Chrome")
            elif err:
                errs.append(err)
        except (capture.Cancelled, capture.Skipped):
            raise
        except Exception as e:
            log.warning("Chrome 渲染失败 %s: %s", url, e)
            errs.append(_short_err(e))
    # 直连之前再问一次 —— 用户可能是在 Chrome 那一段里点的取消/跳过。
    if should_stop and should_stop():
        raise capture.Cancelled("已取消")
    if should_skip and should_skip():
        raise capture.Skipped("已跳过")
    try:
        html = http_get(url)
        bad = browser_error_page(html)
        if bad:
            errs.append(bad + " · 直连")
        else:
            return html, "http", ""
    except Exception as e:
        log.warning("HTTP 抓取失败 %s: %s", url, e)
        errs.append(_short_err(e))
    return "", "", "; ".join(dict.fromkeys(errs)) or "抓取失败"


# 正文短于这个字数就不像「一篇文章」了 —— 通常是付费墙预览、摘要页,或没加载完。
_SHORT = 200

# 用户手动跳过的章节,占位章的说明文字。
# ⚠️ 和「抓取失败」**必须分开写**:一个是他自己点的跳过,一个是真失败了。
# 写成同一句话,他就分不清"是我跳的"还是"它坏了"。
SKIP_NOTE = "已跳过(你点了跳过,可以稍后单独重抓这一章)"


def _quality_note(n):
    """内容抓到了、但看着不像正文时的提示。不阻断导入,只做标记。"""
    if n < _SHORT:
        return f"正文仅 {n} 字,可能只抓到摘要或付费墙预览"
    return ""


def _placeholder(url, title_hint, why):
    """抓取失败也建一章 —— 内容空着,note 写明原因。

    ⚠️ 早先的做法是失败就不建。于是「导入 10 个网址」只出现 7 章,
    用户看到的是「莫名其妙少了三章」,而不是「这三章失败了、原因是这个」。
    失败本身不可怕,不可见才可怕。
    """
    return {"title": (title_hint or url or "抓取失败的页面")[:120],
            "parsed_html": "", "html": None, "url": url, "mode": "",
            "note": why or "抓取失败"}


def article_from_url(url, use_chrome=True, title_hint=None, timeout=60,
                     should_stop=None, should_skip=None):
    """抓一个 URL 并抽正文。

    返回 (art, why)。成功时 why="",art["note"] 里可能带「内容偏短」提示;
    失败时 art=None,why 是可以直接显示给用户看的原因。
    """
    html, via, err = fetch_html(url, use_chrome=use_chrome, timeout=timeout,
                                should_stop=should_stop,
                                should_skip=should_skip)
    if not html:
        return None, (err or "抓取失败")
    art = extract_page.extract_article(html, url, title_hint=title_hint)
    # ⚠️ 本机被反爬挡住时,换**阅读器代理**再试一次(见 reader_get 上面的注释)。
    #    只在「几乎一个字都没抽出来」时才走这条 —— 正常文章不会因此多等一次请求。
    if not art or art["text_len"] < 60:
        rhtml, _rerr = reader_get(url, timeout=min(timeout, 45))
        if rhtml:
            rart = extract_page.extract_article(rhtml, url,
                                                title_hint=title_hint)
            if rart and rart["text_len"] >= 60:
                art, html, via = rart, rhtml, "reader"
                art["method"] = "reader"
                log.info("本机被挡,已通过阅读器代理取到 %s 字:%s",
                         art["text_len"], url)
    if not art or art["text_len"] < 60:
        title = title_hint or extract_page.guess_title(html, url)
        body = sanitize.clean_html(html)
        n = sanitize.plain_len(body)
        if n < 60:
            why = "页面需要登录,或正文无法解析"
            # 顺序有讲究:**先问扩展**,因为它给的是**定论**;
            # 反爬拦截和"要登录"都只是我们从页面长相上猜的。
            if _BPC_NOFIX.search(html):
                why = "这个网站的付费墙绕不过去(扩展报了 no fix)—— 换个来源,或先在这个网站上登录"
            elif _JUNK.search(html[:8000]):
                why = "被反爬拦截(试试开启 Chrome 渲染后重新获取)"
            return None, why
        art = {"title": title, "content": body, "text_len": n,
               "author": None, "method": "raw"}
    art["url"] = url
    art["via"] = via
    art["mode"] = f"{art['method']}/{via}"
    art["note"] = _quality_note(art["text_len"])
    return art, ""


# ------------------------------------------------------------ 多个网址 ------
def import_urls(ctx, cid, urls, use_chrome=True, timeout=60, workers=3):
    """导入一批网址。

    ⚠️ **并发**。原先是一个一个串行抓,每个链接光"等页面稳定"就 2 秒起,
    再加加载和取整页 HTML —— 20 个链接要两三分钟,而且期间界面只有一个
    缓慢跳动的计数,用户会觉得卡死了(「如果卡壳了我就没有任何办法」)。
    现在同时开 3 个标签页抓,进度按**完成数**报,取消也在每一轮轮询里检查。
    """
    # ⚠️ 必须走 normalize_urls —— 用户粘进来的常常是 [标题](网址) 这种
    #    Markdown 链接,直接当网址用会全部「抓取失败」
    urls = normalize_urls(urls)
    if not urls:
        ctx.log("没解析出任何网址 —— 检查一下粘贴的内容里有没有 http(s):// 开头的链接")
        return {"added": 0, "failed": [], "total": 0}
    ctx.set_total(len(urls))
    nw = max(1, min(int(workers or 3), 6))
    ctx.log(f"开始导入 {len(urls)} 个网址"
            f"(Chrome 渲染:{'开' if use_chrome else '关'},同时 {nw} 个)")

    # 先把浏览器和扩展准备好,别让 3 个线程同时去启动它
    if use_chrome:
        try:
            ok, err = capture.ensure()
            if not ok:
                ctx.log(f"浏览器没起来({err}),会退回普通 HTTP 抓取")
        except Exception as e:
            ctx.log(f"浏览器准备失败({e}),会退回普通 HTTP 抓取")

    results, lock = {}, threading.Lock()

    def one(i, u):
        if ctx.should_stop():
            with lock:
                results[i] = (u, None, "已取消")
            return
        ctx.begin_item(i)          # ← 登记在途,「跳过」靠它挑出跑得最久的那条
        try:
            art, why = article_from_url(u, use_chrome=use_chrome,
                                        timeout=timeout,
                                        should_stop=ctx.should_stop,
                                        should_skip=lambda: ctx.should_skip(i))
        except capture.Cancelled:
            with lock:
                results[i] = (u, None, "已取消")
            return
        except capture.Skipped:
            with lock:
                results[i] = (u, None, SKIP_NOTE)
                n = len(results)
                ctx.progress(n, len(urls), f"[{n}/{len(urls)}] 已跳过 {u[:60]}")
                ctx.log(f"⏭ 已跳过 {u[:70]}")
            return
        finally:
            ctx.end_item(i)
        with lock:
            results[i] = (u, art, why)
            n = len(results)
            ctx.progress(n, len(urls), f"[{n}/{len(urls)}] {u[:72]}")
            if art:
                ctx.log(f"✓ {art['title'][:60]} "
                        f"({art['text_len']} 字,{art['via']})")
            else:
                ctx.log(f"✗ {u[:70]} —— {why}")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=nw) as ex:
        list(ex.map(lambda p: one(*p), list(enumerate(urls))))

    # 按**原始顺序**组装,和用户粘进来的次序一致
    items, failed = [], []
    for i, u in enumerate(urls):
        _u, art, why = results.get(i, (u, None, "未处理"))
        if art:
            items.append({"title": art["title"], "parsed_html": art["content"],
                          "html": None, "url": u, "mode": art["mode"],
                          "note": art.get("note") or ""})
        else:
            items.append(_placeholder(u, None, why))
            failed.append({"url": u, "why": why})
    ids = store.add_articles_bulk(cid, items, source="urls")
    return {"added": len(ids), "failed": failed, "total": len(urls)}


# --------------------------------------------------------- 网站内链接 -------
def website_links(url, use_chrome=True, same_domain=True, mode="links",
                  timeout=60):
    """列出站点里可导入的链接(页面内 <a> 或 sitemap.xml)。"""
    if mode == "sitemap" or url.rstrip("/").endswith(".xml"):
        urls = extract_page.sitemap_urls(url, timeout=min(timeout, 40))
        return [{"href": u, "title": u} for u in urls]
    html, via, _err = fetch_html(url, use_chrome=use_chrome, timeout=timeout)
    if not html:
        return []
    return extract_page.page_links(html, url, same_domain=same_domain)


def import_links(ctx, cid, links, use_chrome=True, timeout=60, workers=3):
    """links: [{href,title}]

    ⚠️ **这里原来是一个一个串行抓的**,而用户报的「很多链接的时候特别慢」
    走的正是这条路(站点内链接 / sitemap 动辄几十上百条)。
    更糟的是它调 article_from_url 时**没传 should_stop** —— 单个卡住的页面
    能把这一批拖满整个 timeout,而「取消」要等当前这个跑完才生效
    (用户原话:「如果卡壳了我就没有任何办法」)。
    现在和 import_urls 一样:并行 + 每一轮轮询都能取消。
    """
    links = [l for l in (links or []) if l.get("href")]
    if not links:
        ctx.log("没有可导入的链接")
        return {"added": 0, "failed": [], "total": 0}
    ctx.set_total(len(links))
    nw = max(1, min(int(workers or 3), 6))
    ctx.log(f"开始导入 {len(links)} 个页面链接(同时 {nw} 个)")

    if use_chrome:
        try:
            ok, err = capture.ensure()
            if not ok:
                ctx.log(f"浏览器没起来({err}),会退回普通 HTTP 抓取")
        except Exception as e:
            ctx.log(f"浏览器准备失败({e}),会退回普通 HTTP 抓取")

    results, lock = {}, threading.Lock()

    def one(i, l):
        u = l["href"]
        if ctx.should_stop():
            with lock:
                results[i] = (l, None, "已取消")
            return
        ctx.begin_item(i)
        try:
            art, why = article_from_url(u, use_chrome=use_chrome,
                                        title_hint=l.get("title"),
                                        timeout=timeout,
                                        should_stop=ctx.should_stop,
                                        should_skip=lambda: ctx.should_skip(i))
        except capture.Cancelled:
            with lock:
                results[i] = (l, None, "已取消")
            return
        except capture.Skipped:
            with lock:
                results[i] = (l, None, SKIP_NOTE)
                n = len(results)
                ctx.progress(n, len(links), f"[{n}/{len(links)}] 已跳过 {u[:60]}")
                ctx.log(f"⏭ 已跳过 {u[:70]}")
            return
        finally:
            ctx.end_item(i)
        with lock:
            results[i] = (l, art, why)
            n = len(results)
            ctx.progress(n, len(links), f"[{n}/{len(links)}] {u[:80]}")
            if art:
                ctx.log(f"✓ {art['title'][:60]}")
            else:
                ctx.log(f"✗ {u[:70]} —— {why}")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=nw) as ex:
        list(ex.map(lambda p: one(*p), list(enumerate(links))))

    items, failed = [], []
    for i, l in enumerate(links):
        _l, art, why = results.get(i, (l, None, "未处理"))
        u = l["href"]
        if art:
            items.append({"title": art["title"], "parsed_html": art["content"],
                          "html": None, "url": u, "mode": art["mode"],
                          "note": art.get("note") or ""})
        else:
            items.append(_placeholder(u, l.get("title"), why))
            failed.append({"url": u, "why": why})
    ids = store.add_articles_bulk(cid, items, source="website")
    return {"added": len(ids), "failed": failed, "total": len(links)}


# ---------------------------------------------------------------- RSS -------
def rss_preview(url, limit=40):
    """抓 RSS 并返回可勾选的条目(不写库)。"""
    import fulltext
    import requests
    sess = requests.Session()
    import rss2epub
    entries = fulltext.fetch_feed(url, 30, sess)
    out = []
    for e in entries[:limit]:
        link = fulltext.entry_link(e)
        if not link:
            continue
        try:
            summary = rss2epub.entry_summary(e) or ""
        except Exception:
            summary = ""
        out.append({
            "title": (e.get("title") or "(无标题)").strip(),
            "link": link,
            "pub": e.get("published") or e.get("updated") or "",
            "has_full": bool(fulltext.entry_has_full(e)),
            "summary": summary,
        })
    return out


def import_rss_items(ctx, cid, items, use_chrome=True, timeout=60):
    """items: [{title,link,feed_summary?}] —— 逐条取全文。"""
    import fulltext
    items = [i for i in (items or []) if i.get("link")]
    ctx.set_total(len(items))
    ctx.log(f"开始导入 {len(items)} 条 RSS 条目")
    added, failed = [], []
    for i, it in enumerate(items):
        if ctx.should_stop():
            break
        u = it["link"]
        ctx.progress(i, len(items), f"[{i+1}/{len(items)}] {u[:80]}")
        body, mode, detail = "", "", ""
        # 先按 RSS 那套四层策略;若是需要浏览器解锁的站,再用 Chrome 补一次
        try:
            body, mode, detail = fulltext.get_body(
                u, it.get("summary") or "", bool(it.get("has_full")),
                {"browser": {"enabled": False}}, timeout)
        except Exception as e:
            log.debug("fulltext 失败 %s: %s", u, e)
        if (not body or sanitize.plain_len(body) < 300) and use_chrome:
            art, why = article_from_url(u, use_chrome=True,
                                        title_hint=it.get("title"), timeout=timeout)
            if art and art["text_len"] > sanitize.plain_len(body):
                body, mode, detail = art["content"], art["mode"], "Chrome 渲染"
        body = sanitize.clean_html(body)
        if sanitize.plain_len(body) >= 60:
            added.append({"title": it.get("title") or extract_page.guess_title(body, u),
                          "parsed_html": body, "url": u, "mode": mode})
            ctx.log(f"✓ {(it.get('title') or u)[:60]} [{mode}]")
        else:
            failed.append({"url": u, "why": detail or "未取到正文"})
            ctx.log(f"✗ {u[:70]} —— {detail or '未取到正文'}")
    ids = store.add_articles_bulk(cid, added, source="rss")
    return {"added": len(ids), "failed": failed, "total": len(items)}


# ------------------------------------------------------------ Markdown -----
def import_markdown(ctx, cid, text):
    docs = extract_page.split_markdown_docs(text or "")
    ctx.set_total(len(docs))
    items = []
    for i, (title, body) in enumerate(docs):
        if ctx.should_stop():
            break
        ctx.progress(i, len(docs), f"解析 {title[:60]}")
        _t, html = extract_page.markdown_to_html(body)
        items.append({"title": title, "parsed_html": sanitize.clean_html(html),
                      "url": None, "mode": "markdown"})
    ids = store.add_articles_bulk(cid, items, source="markdown")
    return {"added": len(ids), "total": len(docs)}
