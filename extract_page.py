# -*- coding: utf-8 -*-
"""任意网页 → 文章正文 / 页面内链接 / sitemap;以及 Markdown → HTML。

正文提取优先 trafilatura(和 RSS 那条链路用同一个库),
失败时退回"最大文本块"启发式,再退回整页纯文本,保证永远有东西可读。
"""
import logging
import re
import urllib.parse
import urllib.request

log = logging.getLogger("extract_page")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")

MIN_BODY = 120


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def plain_text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


# ------------------------------------------------------------- 正文提取 ------
def _from_trafilatura(html, url):
    try:
        from trafilatura import extract
        return extract(html, url=url, include_images=True, include_links=True,
                       include_formatting=True, output_format="html")
    except Exception as e:
        log.debug("trafilatura 失败: %s", e)
        return None


def _from_largest_block(html):
    """启发式:找文字最多的那个容器。"""
    try:
        from lxml import html as lh
        from lxml import etree
    except Exception:
        return None
    try:
        doc = lh.fromstring(html)
    except Exception:
        return None
    best, best_len = None, 0
    for tag in ("article", "main", "div", "section"):
        for el in doc.iter(tag):
            txt = " ".join(el.itertext())
            n = len(txt.strip())
            if n > best_len:
                best, best_len = el, n
    if best is None or best_len < MIN_BODY:
        return None
    try:
        return etree.tostring(best, encoding="unicode", method="html")
    except Exception:
        return None


def _from_body(html):
    try:
        from lxml import html as lh
        from lxml import etree
        doc = lh.fromstring(html)
        body = doc.find("body")
        if body is None:
            return None
        return etree.tostring(body, encoding="unicode", method="html")
    except Exception:
        return None


def guess_title(html, url=""):
    try:
        from lxml import html as lh
        doc = lh.fromstring(html or "")
        for xp in ('//meta[@property="og:title"]/@content',
                   '//meta[@name="twitter:title"]/@content',
                   '//title/text()', '//h1//text()'):
            r = doc.xpath(xp)
            if r and str(r[0]).strip():
                return re.sub(r"\s+", " ", str(r[0])).strip()[:200]
    except Exception:
        pass
    return (url or "未命名").rstrip("/").split("/")[-1][:120] or "未命名"


def guess_author(html):
    try:
        from lxml import html as lh
        doc = lh.fromstring(html or "")
        for xp in ('//meta[@name="author"]/@content',
                   '//meta[@property="article:author"]/@content',
                   '//meta[@name="byl"]/@content'):
            r = doc.xpath(xp)
            if r and str(r[0]).strip():
                return str(r[0]).strip()[:120]
    except Exception:
        pass
    return None


def extract_article(html, url, title_hint=None):
    """返回 {title, content, text_len, author, method}。content 是已清洗 HTML。"""
    import sanitize as _sz
    if not html:
        return None
    content = _from_trafilatura(html, url)
    method = "trafilatura"
    if not content or len(plain_text(content)) < MIN_BODY:
        alt = _from_largest_block(html)
        if alt and len(plain_text(alt)) > len(plain_text(content or "")):
            content, method = alt, "largest-block"
    if not content or len(plain_text(content)) < MIN_BODY:
        alt = _from_body(html)
        if alt and len(plain_text(alt)) > len(plain_text(content or "")):
            content, method = alt, "body"
    if not content:
        return None
    content = _sz.clean_html(content)
    return {
        "title": title_hint or guess_title(html, url),
        "content": content,
        "text_len": len(plain_text(content)),
        "author": guess_author(html),
        "method": method,
    }


# --------------------------------------------------------- 页面内链接 --------
def page_links(html, base_url, same_domain=True, max_links=400):
    """解析页面里的 <a>,返回 [{href,title}]。"""
    try:
        from lxml import html as lh
    except Exception:
        return []
    try:
        doc = lh.fromstring(html or "")
    except Exception:
        return []
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    out, seen = [], set()
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absu = urllib.parse.urljoin(base_url, href)
        p = urllib.parse.urlparse(absu)
        if p.scheme not in ("http", "https"):
            continue
        absu = urllib.parse.urlunparse((p.scheme, p.netloc, p.path, p.query, "", ""))
        if same_domain and host:
            h = (p.hostname or "").lower()
            if not (h == host or h.endswith("." + host) or host.endswith("." + h)):
                continue
        if absu in seen:
            continue
        seen.add(absu)
        title = re.sub(r"\s+", " ", " ".join(a.itertext())).strip()
        out.append({"href": absu, "title": title or absu})
        if len(out) >= max_links:
            break
    return out


def sitemap_urls(url, timeout=25, limit=1000):
    """读取 sitemap.xml(含 sitemap index),返回 URL 列表。"""
    import requests
    from xml.etree import ElementTree as ET

    def fetch(u):
        r = requests.get(u, headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        return r.content

    def parse(content):
        root = ET.fromstring(content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        if root.tag.endswith("sitemapindex"):
            return [e.text.strip() for e in root.findall(".//sm:loc", ns) if e.text]
        return [e.text.strip() for e in root.findall(".//sm:loc", ns) if e.text]

    try:
        first = parse(fetch(url))
    except Exception as e:
        log.warning("sitemap 解析失败 %s: %s", url, e)
        return []
    # 是 index 的话再展开一层
    if first and all(u.endswith(".xml") or "/sitemap" in u for u in first[:5]):
        out = []
        for sub in first[:12]:
            try:
                out.extend(parse(fetch(sub)))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out[:limit]
    return first[:limit]


# --------------------------------------------------------- Markdown → HTML ---
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITAL = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_MD_CODE = re.compile(r"`([^`]+)`")


def _md_inline(s):
    s = _esc(s)
    s = _MD_CODE.sub(r"<code>\1</code>", s)
    s = _MD_BOLD.sub(r"<strong>\1</strong>", s)
    s = _MD_ITAL.sub(r"<em>\1</em>", s)
    s = _MD_LINK.sub(r'<a href="\2">\1</a>', s)
    return s


def markdown_to_html(md):
    """够用就好的 Markdown 子集:标题/列表/引用/代码块/分割线/段落。"""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out, buf, in_code, list_open = [], [], False, None
    title = None

    def flush_para():
        if buf:
            out.append("<p>" + _md_inline(" ".join(buf).strip()) + "</p>")
            buf.clear()

    def close_list():
        nonlocal list_open
        if list_open:
            out.append(f"</{list_open}>")
            list_open = None

    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            flush_para(); close_list()
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(_esc(raw))
            continue
        if not line.strip():
            flush_para(); close_list()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para(); close_list()
            lvl = len(m.group(1))
            txt = m.group(2).strip()
            if lvl == 1 and title is None:
                title = txt
            out.append(f"<h{lvl}>{_md_inline(txt)}</h{lvl}>")
            continue
        if re.match(r"^\s*([-*_])\s*\1\s*\1", line):
            flush_para(); close_list()
            out.append("<hr/>")
            continue
        m = re.match(r"^\s*([-*+])\s+(.*)$", line)
        if m:
            flush_para()
            if list_open != "ul":
                close_list(); out.append("<ul>"); list_open = "ul"
            out.append(f"<li>{_md_inline(m.group(2))}</li>")
            continue
        m = re.match(r"^\s*(\d+)[.)]\s+(.*)$", line)
        if m:
            flush_para()
            if list_open != "ol":
                close_list(); out.append("<ol>"); list_open = "ol"
            out.append(f"<li>{_md_inline(m.group(2))}</li>")
            continue
        m = re.match(r"^\s*>\s?(.*)$", line)
        if m:
            flush_para(); close_list()
            out.append(f"<blockquote>{_md_inline(m.group(1))}</blockquote>")
            continue
        buf.append(line.strip())
    if in_code:
        out.append("</code></pre>")
    flush_para(); close_list()
    return title, "".join(out)


def split_markdown_docs(md):
    """把一份 markdown 按一级标题拆成多篇(每篇一个章节)。"""
    parts = re.split(r"(?m)^#\s+", md or "")
    docs = []
    for p in parts[1:]:
        lines = p.split("\n")
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        if title or body:
            docs.append((title or "未命名", body))
    if not docs and (md or "").strip():
        t, h = markdown_to_html(md)
        docs.append((t or "未命名", md))
    return docs
