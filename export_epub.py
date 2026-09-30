# -*- coding: utf-8 -*-
"""合集 → EPUB。对标 EpubKit 的导出,但**没有 10 页上限**。

每篇文章 = 一个章节,带独立导航;可选封面/作者/语言/前言;
图片默认**并发**下载并内嵌(Kindle 上离线可读),失败自动跳过不报错,
全过程向界面报进度 —— 早先串行下载 51 张图要 94 秒且毫无反馈,看起来就像卡死。
"""
import html as _htmlmod
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("export_epub")

CSS = """
body { font-family: -apple-system,'PingFang SC','Helvetica Neue',Georgia,serif;
  line-height:1.7; color:#1a1a1a; margin:1em; }
h1, h2, h3 { line-height:1.35; }
img { max-width:100%; height:auto; }
a { color:#2a6bb0; word-break:break-all; }
blockquote { margin:.7em 1.3em; color:#444; border-left:3px solid #ccc;
  padding-left:.9em; }
pre { background:#f6f8fa; padding:1rem; border-radius:8px; overflow-x:auto; }
code { background:#f6f8fa; padding:.1em .3em; border-radius:4px; }
table { border-collapse:collapse; }
td, th { border:1px solid #ddd; padding:.35em .6em; }
.titlepage { text-align:center; margin-top:22vh; }
.titlepage h1 { font-size:2.2em; }
.titlepage p { color:#666; }
p.meta { font-size:.8em; color:#666; margin:.2em 0 1em; }
nav.topbar { font-size:.84em; padding:.5em .75em; margin:0 0 1.1em;
  border:1px solid #e4e4e4; border-radius:8px; background:#fafafa; }
nav.topbar a.nb { color:#2a6bb0; text-decoration:none; }
nav.topbar span.sep { color:#c4c4c4; margin:0 .55em; }
"""

# 正文开头的标题标签(抽取器经常把页面 <h1> 一起收进来)
_LEAD_HEAD = re.compile(r"^\s*<h([1-4])\b[^>]*>(.*?)</h\1>\s*", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_dup_title(body, title):
    """正文开头如果已经有一模一样的标题,把它去掉。

    ⚠️ 抽取器(trafilatura / readability)常把页面上的 <h1> 标题一起收进正文,
    而导出时我们**又**在正文前面加一个 <h2> —— 阅读器里同一篇文章的标题
    出现两次。用户说的「有时候重复」,是因为带不带取决于那一页的结构。

    只在**文字确实与标题相同**时才删,避免误删正文自己的小标题。
    """
    m = _LEAD_HEAD.match(body or "")
    if not m:
        return body
    head = _htmlmod.unescape(_TAGS.sub("", m.group(2)))
    a = _WS.sub("", head).lower()
    b = _WS.sub("", str(title or "")).lower()
    if not a or not b:
        return body
    if a == b or (len(b) >= 8 and (a.startswith(b) or b.startswith(a))):
        return body[m.end():]
    return body


def _nav_bar(i, total):
    """章节顶部的「上一篇 / 目录 / 下一篇」。

    RSS 每日书一直有这套导航,合集书以前完全没有 —— 在阅读器里读完一篇
    想接着看下一篇,只能退出去点目录再翻。放在**章节最前面**,不用滚到底。
    """
    parts = []
    if i > 0:
        parts.append(f'<a class="nb" href="ch{i - 1}.xhtml">‹ 上一篇</a>')
    parts.append('<a class="nb" href="nav.xhtml">☰ 目录</a>')
    if i < total - 1:
        parts.append(f'<a class="nb" href="ch{i + 1}.xhtml">下一篇 ›</a>')
    return ('<nav class="topbar">'
            + '<span class="sep">|</span>'.join(parts) + '</nav>')


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _xhtml(title, body):
    return ('<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n'
            f'<head><title>{_esc(title)}</title></head>\n'
            f'<body>{body}</body></html>')


_UA_DESKTOP = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")


def _fetch_image(url, timeout=15):
    """取一张图,返回 (内容, Content-Type, 失败原因)。

    ⚠️ 图片下不下来**绝大多数不是"图没了"**,而是对方校验 Referer / UA(防盗链)。
    同一个 URL 带 Referer 能下、不带 Referer 也能下 —— 只试一次就把这张图判死,
    书里就永远缺这一张,而用户完全不知道少了什么。
    所以这里按三个梯队退着试,并把最后一次的原因带回去给界面看。
    """
    import requests
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    tries = (
        {"User-Agent": _UA_DESKTOP, "Referer": origin},
        {"User-Agent": _UA_DESKTOP},                       # 有的站反而讨厌 Referer
        {"User-Agent": "Mozilla/5.0 (compatible; CEVTUO-RWP/1.3)"},
    )
    why = "未知原因"
    for hdr in tries:
        try:
            r = requests.get(url, timeout=timeout, headers=hdr)
            if r.status_code == 200 and r.content:
                return r.content, r.headers.get("Content-Type", ""), ""
            why = f"HTTP {r.status_code}"
        except Exception as e:
            why = str(e)[:90]
    return None, "", why


EXT_MT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}
_IMG_SRC = re.compile(r'<img[^>]+src\s*=\s*"([^"]+)"', re.I)


class ImageEmbedder:
    """把正文里的远程图片并发下载下来内嵌进书。"""

    def __init__(self, limit=800, on_progress=None, workers=12):
        self.limit = max(0, int(limit))
        self.workers = max(1, int(workers))
        self.on_progress = on_progress
        self.map = {}
        self.blobs = []
        self.failed = []          # [{url, why}] —— 要给用户看的"没下到"名单
        self._lk = threading.Lock()

    def report(self):
        """这次一共要下几张、下到几张、哪几张没下到。"""
        want = len(self.map)
        got = len(self.blobs)
        return {"wanted": want, "ok": got,
                "failed": sorted(self.failed, key=lambda x: x["url"])}

    def collect(self, htmls):
        """先把所有要下的图片 URL 收集去重(最多 limit 张)。"""
        urls, seen = [], set()
        for h in htmls:
            for m in _IMG_SRC.finditer(h or ""):
                u = m.group(1)
                if not u or u.startswith(("data:", "images/")) or u in seen:
                    continue
                seen.add(u)
                urls.append(u)
                if len(urls) >= self.limit:
                    return urls
        return urls

    def download(self, urls, should_stop=None):
        """并发下载,边下边报进度。"""
        if not urls:
            return
        done = 0
        total = len(urls)
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = {ex.submit(self._get, u): u for u in urls}
            for fu in as_completed(futs):
                done += 1
                if should_stop and should_stop():
                    for f in futs:
                        f.cancel()
                    break
                if self.on_progress and (done % 4 == 0 or done == total):
                    self.on_progress(done, total, f"下载图片 {done}/{total}")

    def _get(self, url):
        with self._lk:
            if url in self.map:
                return self.map[url]
        content, ctype, why = _fetch_image(url)
        if not content:
            with self._lk:
                self.map[url] = None
                # ⚠️ 以前这里是静默跳过。结果是「书里少了几张图」而用户
                #    永远不知道少了什么、为什么少 —— 必须记下来报出去。
                self.failed.append({"url": url, "why": why or "下载失败"})
            return None
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if ext not in EXT_MT:
            ext = {"image/png": ".png", "image/gif": ".gif",
                   "image/webp": ".webp", "image/svg+xml": ".svg"}.get(
                       ctype.split(";")[0].strip(), ".jpg")
        mt = EXT_MT.get(ext, "image/jpeg")
        with self._lk:
            if url in self.map:
                return self.map[url]
            uid = f"img{len(self.blobs) + 1}"
            fn = f"images/{uid}{ext}"
            self.map[url] = fn
            self.blobs.append((uid, fn, mt, content))
        return fn

    def rewrite(self, html):
        if not html or self.limit <= 0:
            return html
        from lxml import html as lh
        from lxml import etree
        try:
            doc = lh.fromstring(f"<div>{html}</div>")
        except Exception:
            return html
        for img in doc.xpath("//img"):
            src = img.get("src")
            fn = self.map.get(src) if src else None
            if fn:
                img.set("src", fn)
                for a in ("srcset", "data-src", "data-original", "sizes",
                          "loading"):
                    img.attrib.pop(a, None)
            elif src and not src.startswith(("data:", "images/")):
                # 下载失败的图:去掉,免得 Kindle 上留一堆破图
                img.getparent().remove(img)
        try:
            return "".join(etree.tostring(c, encoding="unicode", method="html")
                           for c in doc)
        except Exception:
            return html


def build(collection, articles, out_path, opts=None, on_progress=None):
    """生成 EPUB。

    collection: {title, author, language_code, cover}
    articles:   [{title, parsed_html, url, created_at}]
    opts:       {remove_preface, embed_images, max_images}
    on_progress(done, total, msg=None)
    """
    from ebooklib import epub

    def report(done, total, msg=None):
        if on_progress:
            try:
                on_progress(done, total, msg)
            except TypeError:
                on_progress(done, total)

    opts = opts or {}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    title = collection.get("title") or "未命名合集"
    author = collection.get("author") or "cev"
    lang = collection.get("language_code") or "zh-CN"

    usable = [a for a in articles if (a.get("parsed_html") or "").strip()]
    if not usable:
        raise RuntimeError("empty-collection:这个合集里还没有任何正文,先导入文章")

    book = epub.EpubBook()
    book.set_identifier(f"cevtuo-{uuid.uuid4().hex}")
    book.set_title(title)
    book.set_language(lang)
    book.add_author(author)
    book.add_item(epub.EpubItem(uid="css", file_name="style.css",
                                media_type="text/css",
                                content=CSS.encode("utf-8")))

    def style(item):
        item.add_link(href="style.css", rel="stylesheet", type="text/css")
        return item

    cover_path = collection.get("cover")
    if cover_path and Path(cover_path).exists():
        try:
            ext = Path(cover_path).suffix.lower() or ".jpg"
            book.set_cover(f"cover{ext}", Path(cover_path).read_bytes())
        except Exception as e:
            log.warning("封面读取失败: %s", e)
    else:
        # 没有封面就生成一张「只有书名和作者」的。
        # 书架上一本光秃秃的书,比一本印着名字的素封面难认多了 ——
        # 用户的要求就是「没有封面,那就只写书名和作者」。
        try:
            import coverart
            book.set_cover("cover.jpg", coverart.generate(title, author))
        except Exception as e:
            log.warning("生成封面失败: %s", e)

    # ---- 图片:先全部并发下载,再写章节 ----
    embedder = ImageEmbedder(limit=int(opts.get("max_images") or 800),
                             on_progress=report)
    if opts.get("embed_images", True):
        bodies = [a.get("parsed_html") or "" for a in usable]
        urls = embedder.collect(bodies)
        if urls:
            report(0, len(urls), f"准备下载 {len(urls)} 张图片")
            embedder.download(urls)

    chapters, spine = [], []
    total = len(usable)
    for i, a in enumerate(usable):
        body = a.get("parsed_html") or ""
        if opts.get("embed_images", True):
            body = embedder.rewrite(body)
        elink = a.get("url") or ""
        meta = []
        if a.get("created_at"):
            meta.append(_esc(a["created_at"][:10]))
        if elink:
            meta.append(f'<a href="{_esc(elink)}">原文链接</a>')
        body = _strip_dup_title(body, a.get("title"))
        inner = (_nav_bar(i, total)
                 + f'<h2>{_esc(a.get("title") or "未命名")}</h2>'
                 + (f'<p class="meta">{" · ".join(meta)}</p>' if meta else "")
                 + f'<div class="content">{body}</div>')
        ch = epub.EpubHtml(uid=f"ch{i}", file_name=f"ch{i}.xhtml",
                           title=(a.get("title") or f"第 {i+1} 篇")[:120])
        ch.content = _xhtml(a.get("title") or f"第 {i+1} 篇", inner)
        book.add_item(style(ch))
        chapters.append(ch)
        spine.append(ch)
        report(i + 1, total, f"写入章节 {i+1}/{total}")

    for uid, fn, mt, blob in embedder.blobs:
        book.add_item(epub.EpubImage(uid=uid, file_name=fn, media_type=mt,
                                     content=blob))

    # 扉页 —— 只放标题,不再内嵌一份目录。
    # 早先这里既生成扉页里的 <ol> 目录、又让 ebooklib 生成 nav 目录,
    # 阅读器里就会出现「两次目录」。现在只保留 nav 那一份。
    if not opts.get("remove_preface", False):
        preface = epub.EpubHtml(uid="preface", file_name="preface.xhtml",
                                title="扉页")
        preface.content = _xhtml("扉页", (
            f'<div class="titlepage"><h1>{_esc(title)}</h1>'
            f'<p>{_esc(author)}</p>'
            f'<p>{len(usable)} 篇 · 由 CEVTUO-RWP 制作</p></div>'))
        book.add_item(style(preface))
        spine.insert(0, preface)

    # 唯一的目录:交给 ebooklib 的 nav(ncx 供老设备)
    book.toc = tuple(chapters)
    book.spine = ["nav"] + spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    report(total, total, "打包 epub…")
    epub.write_epub(str(out_path), book)
    # 把图片下载的账一并交出去 —— 用户要看「这次下了多少张、哪些没下到」,
    # 以前这里只回一个路径,等于没说。
    rep = embedder.report()
    rep["path"] = str(out_path)
    return rep


def build_linear(book_title, date_str, sections, images, out_path,
                 author="CEVTUO-RWP"):
    """RSS 连续流书(复用 rss2epub.LinearBook 的版式)。"""
    import rss2epub
    lb = rss2epub.LinearBook(book_title, date_str, sections, images)
    return lb.build(Path(out_path))
