# -*- coding: utf-8 -*-
"""导入流水线:多个网址 / 网站内链接 / RSS / Markdown → 合集章节。

取正文默认走**真实 Chrome profile 副本渲染**(capture.render),
所以 archive.today、以及靠 Bypass Paywalls Clean 才解锁的页面都能拿到全文;
Chrome 不可用时自动退回普通 HTTP 抓取,并在结果里标注来源。
"""
import logging
import re

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


def http_get(url, timeout=25):
    import requests
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout,
                     allow_redirects=True)
    r.raise_for_status()
    return r.text


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
    return ""


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


def fetch_html(url, use_chrome=True, timeout=60):
    """返回 (html, via, err)。via ∈ chrome | http | ''(失败)。"""
    errs = []
    if use_chrome:
        try:
            html, err = capture.render(url, timeout=timeout)
            if html and len(html) > 400:
                bad = browser_error_page(html)
                if not bad:
                    return html, "chrome", ""
                errs.append(bad + " · Chrome")
            elif err:
                errs.append(err)
        except Exception as e:
            log.warning("Chrome 渲染失败 %s: %s", url, e)
            errs.append(_short_err(e))
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


def article_from_url(url, use_chrome=True, title_hint=None, timeout=60):
    """抓一个 URL 并抽正文。

    返回 (art, why)。成功时 why="",art["note"] 里可能带「内容偏短」提示;
    失败时 art=None,why 是可以直接显示给用户看的原因。
    """
    html, via, err = fetch_html(url, use_chrome=use_chrome, timeout=timeout)
    if not html:
        return None, (err or "抓取失败")
    art = extract_page.extract_article(html, url, title_hint=title_hint)
    if not art or art["text_len"] < 60:
        title = title_hint or extract_page.guess_title(html, url)
        body = sanitize.clean_html(html)
        n = sanitize.plain_len(body)
        if n < 60:
            why = "页面需要登录,或正文无法解析"
            if _JUNK.search(html[:8000]):
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
def import_urls(ctx, cid, urls, use_chrome=True, timeout=60):
    urls = [u.strip() for u in urls if u and u.strip()]
    ctx.set_total(len(urls))
    ctx.log(f"开始导入 {len(urls)} 个网址(Chrome 渲染:{'开' if use_chrome else '关'})")
    items, failed = [], []
    for i, u in enumerate(urls):
        if ctx.should_stop():
            break
        ctx.progress(i, len(urls), f"[{i+1}/{len(urls)}] {u[:80]}")
        art, why = article_from_url(u, use_chrome=use_chrome, timeout=timeout)
        if art:
            items.append({"title": art["title"], "parsed_html": art["content"],
                          "html": None, "url": u, "mode": art["mode"],
                          "note": art.get("note") or ""})
            ctx.log(f"✓ {art['title'][:60]} ({art['text_len']} 字,{art['via']})")
        else:
            items.append(_placeholder(u, None, why))
            failed.append({"url": u, "why": why})
            ctx.log(f"✗ {u[:70]} —— {why}")
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


def import_links(ctx, cid, links, use_chrome=True, timeout=60):
    """links: [{href,title}]"""
    links = [l for l in (links or []) if l.get("href")]
    ctx.set_total(len(links))
    ctx.log(f"开始导入 {len(links)} 个页面链接")
    items, failed = [], []
    for i, l in enumerate(links):
        if ctx.should_stop():
            break
        u = l["href"]
        ctx.progress(i, len(links), f"[{i+1}/{len(links)}] {u[:80]}")
        art, why = article_from_url(u, use_chrome=use_chrome,
                                    title_hint=l.get("title"), timeout=timeout)
        if art:
            items.append({"title": art["title"], "parsed_html": art["content"],
                          "html": None, "url": u, "mode": art["mode"],
                          "note": art.get("note") or ""})
            ctx.log(f"✓ {art['title'][:60]}")
        else:
            items.append(_placeholder(u, l.get("title"), why))
            failed.append({"url": u, "why": why})
            ctx.log(f"✗ {u[:70]} —— {why}")
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
