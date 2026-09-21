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


def fetch_html(url, use_chrome=True, timeout=60):
    """返回 (html, via)。via ∈ chrome | http | ''(失败)。"""
    if use_chrome:
        try:
            html, err = capture.render(url, timeout=timeout)
            if html and len(html) > 400:
                return html, "chrome"
        except Exception as e:
            log.warning("Chrome 渲染失败 %s: %s", url, e)
    try:
        return http_get(url), "http"
    except Exception as e:
        log.warning("HTTP 抓取失败 %s: %s", url, e)
        return "", ""


def article_from_url(url, use_chrome=True, title_hint=None, timeout=60):
    """抓一个 URL 并抽正文 → dict,失败返回 (None, 原因)。"""
    html, via = fetch_html(url, use_chrome=use_chrome, timeout=timeout)
    if not html:
        return None, "抓取失败"
    art = extract_page.extract_article(html, url, title_hint=title_hint)
    if not art or art["text_len"] < 60:
        title = title_hint or extract_page.guess_title(html, url)
        body = sanitize.clean_html(html)
        if sanitize.plain_len(body) < 60:
            why = "页面需要登录或无法解析"
            if _JUNK.search(html[:8000]):
                why = "被反爬拦截(请开启 Chrome 渲染)"
            return None, why
        art = {"title": title, "content": body,
               "text_len": sanitize.plain_len(body), "author": None,
               "method": "raw"}
    art["url"] = url
    art["via"] = via
    art["mode"] = f"{art['method']}/{via}"
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
                          "html": None, "url": u, "mode": art["mode"]})
            ctx.log(f"✓ {art['title'][:60]} ({art['text_len']} 字,{art['via']})")
        else:
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
    html, via = fetch_html(url, use_chrome=use_chrome, timeout=timeout)
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
                          "html": None, "url": u, "mode": art["mode"]})
            ctx.log(f"✓ {art['title'][:60]}")
        else:
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
