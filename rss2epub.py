#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日 RSS → EPUB(连续流版式)。

- 正文:fulltext.get_body() 四层合法取全文 + 外接 + 兜底摘要,模式写进统计。
- 版式:封面 + 「今日源目录」索引页;正文按 分类→源→文章 连续铺开(Kindle 直接翻页阅读,
  不必先点目录)。每篇文章顶部一条极窄导航:←上篇 · 目录 · 下篇→ · 上一/下一源 · 回源列表。
- 产物:output/YYYY-MM-DD/ 下每分类一本;--send-email 时另出合并本并邮件推送。
- 统计:history/YYYY-MM-DD.json;进度:state/progress.json(供 Web UI 轮询)。
"""
import argparse
import calendar
import json
import logging
import os
import re
import smtplib
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests

import fulltext
from fulltext import net_get, _plain_len, FULL_MODES

log = logging.getLogger("rss2epub")
DEFAULT_CATEGORY = "未分类"
MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
               ".avif": "image/avif"}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")


# ---------------------------------------------------------------- OPML/state
def parse_opml(path):
    tree = ET.parse(path)
    body = tree.getroot().find("body")
    cats, loose = [], []
    if body is None:
        return cats
    for out in body.findall("outline"):
        if out.get("xmlUrl"):
            loose.append((out.get("title") or out.get("text") or "", out.get("xmlUrl")))
            continue
        feeds = [(o.get("title") or o.get("text") or "", o.get("xmlUrl"))
                 for o in out.iter("outline") if o.get("xmlUrl")]
        if feeds:
            cats.append((out.get("text") or out.get("title") or DEFAULT_CATEGORY, feeds))
    if loose:
        for n, f in cats:
            if n == DEFAULT_CATEGORY:
                f.extend(loose)
                loose = []
        if loose:
            cats.append((DEFAULT_CATEGORY, loose))
    return cats


def clean_title(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def sanitize_filename(name, fallback="epub"):
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip().rstrip(".")
    return name or fallback


def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html or "").strip()


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class Seen:
    def __init__(self, path: Path, prune_days: int = 45):
        self.path = path
        self._d = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                if "\t" in line:
                    ts, url = line.split("\t", 1)
                    self._d[url] = float(ts)
                else:
                    self._d[line] = time.time()
        cutoff = time.time() - prune_days * 86400
        self._d = {u: t for u, t in self._d.items() if t >= cutoff}

    def contains(self, url):
        return url in self._d

    def mark(self, url):
        self._d[url] = time.time()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = "\n".join(f"{int(t)}\t{u}" for u, t in sorted(self._d.items()))
        self.path.write_text(lines + "\n", encoding="utf-8")


# ---------------------------------------------------------------- html utils
def sanitize_html(raw: str) -> str:
    if not raw:
        return ""
    from lxml import etree, html as lh
    from lxml_html_clean import clean_html
    try:
        doc = lh.document_fromstring(raw)
    except Exception:
        return "<p>%s</p>" % _esc(strip_tags(raw))
    try:
        clean_html(doc)
    except Exception:
        pass
    for tag in doc.xpath("//script|//style|//iframe|//object|//embed|//form|//head"):
        tag.drop_tree()
    for el in doc.iter():
        for a in list(el.attrib):
            if a.startswith("on") or a == "style":
                del el.attrib[a]
            elif a == "href" and el.attrib[a].lstrip().lower().startswith("javascript:"):
                del el.attrib[a]
            elif a == "src" and el.attrib[a].lstrip().lower().startswith("javascript:"):
                del el.attrib[a]
    body = doc.body
    if body is None:
        return ""
    return "".join(etree.tostring(c, encoding="unicode", method="html")
                   for c in body)


def downscale_image(content, ext, media_type, max_width=1400, max_bytes=220_000):
    try:
        from io import BytesIO
        from PIL import Image
    except Exception:
        return content, ext, media_type
    if media_type == "image/svg+xml" or ext == ".gif":
        return content, ext, media_type
    if len(content) <= max_bytes:
        return content, ext, media_type
    try:
        img = Image.open(BytesIO(content))
        img.load()
    except Exception:
        return content, ext, media_type
    w, h = img.size
    try:
        if w > max_width:
            h = max(1, round(h * max_width / w))
            w = max_width
        img = img.convert("RGB")
        img = img.resize((w, h), Image.LANCZOS)
        out = BytesIO()
        img.save(out, format="JPEG", quality=76, optimize=True)
        if out.tell() >= len(content):
            return content, ext, media_type
        return out.getvalue(), ".jpg", "image/jpeg"
    except Exception:
        return content, ext, media_type


class ImagePool:
    """线程安全、按 URL 去重、可并发的图片下载池。"""

    def __init__(self, sess, timeout, per_article, total_limit, cookie_box=None):
        self.sess = sess
        self.timeout = timeout
        self.per_article = per_article
        self.remaining = total_limit
        self.map = {}
        self.blobs = []
        self._box = cookie_box
        self._lk = threading.Lock()

    def resolve(self, html: str) -> str:
        if not html:
            return html
        from lxml import html as lh
        from lxml import etree
        try:
            doc = lh.fromstring(html)
        except Exception:
            return html
        count = 0
        for img in doc.xpath("//img"):
            if count >= self.per_article:
                break
            src = img.get("src") or img.get("data-original") or img.get("data-src")
            if not src or src.lower().startswith("data:"):
                continue
            fn = self._fetch(src)
            if fn:
                img.set("src", fn)
                for a in ("srcset", "data-original", "data-src", "data-lazy-src", "sizes"):
                    img.attrib.pop(a, None)
                count += 1
        try:
            return etree.tostring(doc, encoding="unicode", method="html")
        except Exception:
            return html

    def _fetch(self, url):
        with self._lk:
            if url in self.map:
                return self.map[url]
            if self.remaining <= 0:
                return None
        cookies = self._box.for_url(url) if self._box else None
        r = None
        for attempt in range(2):                       # 失败重试一次,换 UA
            try:
                from fulltext import _ALT_UAS
                headers = _ALT_UAS[attempt] if attempt else None
                r = net_get(url, self.timeout, cookies, headers=headers)
                break
            except Exception:
                r = None
        if r is None or r.status_code != 200 or not r.content:
            return None
        with self._lk:
            if url in self.map:                 # 可能已被别的线程加入
                return self.map[url]
            if self.remaining <= 0:
                return None
            ext = os.path.splitext(urlparse(url).path)[1].lower()
            if ext not in MIME_BY_EXT:
                ext = ".jpg"
            content, ext, mt = downscale_image(r.content, ext, MIME_BY_EXT[ext])
            uid = f"img{len(self.blobs) + 1}"
            fn = f"images/{uid}{ext}"
            self.map[url] = fn
            self.blobs.append((uid, fn, mt, content))
            self.remaining -= 1
            return fn


# ---------------------------------------------------------------- 数据模型
@dataclass
class Article:
    feed_title: str
    feed_url: str
    title: str
    link: str
    pub: str
    raw_html: str = ""          # feed 自带原文(未清洗)
    feed_has_full: bool = False
    body: str = ""
    mode: str = ""
    detail: str = ""


# ---------------------------------------------------------------- 连续流 builder
CSS = """
body { font-family: -apple-system,'PingFang SC','Helvetica Neue',Georgia,serif;
  line-height:1.7; color:#1a1a1a; margin:1em; }
div.content img { max-width:100%; height:auto; margin:.6em 0; }
div.content p { margin:.75em 0; text-align:justify; }
div.content a { color:#2a6bb0; word-break:break-all; }
div.content blockquote { margin:.7em 1.3em; color:#444;
  border-left:3px solid #ccc; padding-left:.9em; }
/* 顶部导航窄条 */
nav.topbar { font-size:.7em; line-height:1.5; color:#777; padding:.12em .5em;
  background:#f3f3f3; border-bottom:1px solid #ddd; margin-bottom:.6em; }
nav.topbar a { color:#2a6bb0; text-decoration:none; margin:0 .15em; }
nav.topbar .sep { color:#bbb; margin:0 .3em; }
nav.topbar .src { color:#999; }
h1.cat { font-size:1.8em; border-bottom:2px solid #444; padding-bottom:.15em; }
h2.feed { font-size:1.25em; margin:1.6em 0 .3em; border-bottom:1px solid #bbb;
  padding-bottom:.1em; }
h3.art { font-size:1.1em; margin:.1em 0 .2em; }
p.meta { font-size:.8em; color:#666; margin:0 0 .5em; }
a.part { color:#888; font-size:.75em; }
.sources ul { list-style:none; padding-left:.5em; }
.sources li { margin:.25em 0; }
.sources .cat { font-size:1.3em; margin-top:1.2em; border-bottom:1px solid #bbb; }
.titlepage { text-align:center; margin-top:22vh; }
.titlepage h1 { font-size:2.2em; } .titlepage p { color:#666; }
hr.artend { border:0; border-top:1px dashed #ddd; margin:1.6em 0; }
"""


def _xhtml(title, body_html):
    return (f"<!DOCTYPE html>\n<html xmlns=\"http://www.w3.org/1999/xhtml\">\n"
            f"<head><title>{_esc(title)}</title></head>\n"
            f"<body>{body_html}</body></html>")


class LinearBook:
    """连续流书。sections = [Root{name, file, feeds:[Feed{...articles}]}, ...]。"""

    def __init__(self, book_title, date_str, sections, images):
        self.title = book_title
        self.date = date_str
        self.sections = sections
        self.images = images
        # 预分配锚点 id
        self.aid = {}
        self.fid = {}
        n = 0
        self.order = []          # (section, feed, article)
        for si, sec in enumerate(sections):
            for fi, feed in enumerate(sec["feeds"]):
                self.fid[(si, fi)] = f"f{n}"
                n += 1
        n = 0
        for si, sec in enumerate(sections):
            for fi, feed in enumerate(sec["feeds"]):
                for ai, a in enumerate(feed["articles"]):
                    self.aid[(si, fi, ai)] = f"a{n}"
                    self.order.append((si, fi, ai))
                    n += 1
        # 源在书内的顺序(用于 上一源/下一源)
        feed_first = {}
        for pos, (s, f, a) in enumerate(self.order):
            feed_first.setdefault((s, f), pos)
        self.feed_order = sorted(feed_first, key=lambda k: feed_first[k])
        self.feed_idx = {k: i for i, k in enumerate(self.feed_order)}

    def _feed_neighbors(self, si, fi):
        i = self.feed_idx[(si, fi)]
        out = {}
        if i > 0:
            s, f = self.feed_order[i - 1]
            out["prev"] = (s, f, self.sections[s]["feeds"][f]["name"],
                           self.fid[(s, f)])
        if i < len(self.feed_order) - 1:
            s, f = self.feed_order[i + 1]
            out["next"] = (s, f, self.sections[s]["feeds"][f]["name"],
                           self.fid[(s, f)])
        return out

    def _pos(self, si, fi, ai):
        for idx, (s, f, a) in enumerate(self.order):
            if (s, f, a) == (si, fi, ai):
                return idx
        return -1

    def build(self, out_path):
        from ebooklib import epub
        book = epub.EpubBook()
        book.set_identifier(f"rssdaily-{uuid.uuid4().hex}")
        book.set_title(self.title)
        book.set_language("zh-CN")
        book.add_author("RSS Daily Digest")
        book.add_item(epub.EpubItem(uid="css", file_name="style.css",
                                    media_type="text/css", content=CSS.encode("utf-8")))

        def style(item):
            item.add_link(href="style.css", rel="stylesheet", type="text/css")
            return item

        cover = epub.EpubHtml(uid="cover", file_name="cover.xhtml", title=self.title)
        cover.content = _xhtml(self.title,
                               f"<div class=\"titlepage\"><h1>{_esc(self.title)}</h1>"
                               f"<p>{_esc(self.date)} · {len(self.order)} 篇</p></div>")
        book.add_item(style(cover))

        # 源目录索引页(也承担“回源选择”)
        toc_entries = []
        src = [f"<div class=\"sources\" id=\"top\"><h1>{_esc(self.date)} 全部源</h1>"]
        for si, sec in enumerate(self.sections):
            src.append(f"<h2 class=\"cat\">{_esc(sec['name'])}</h2><ul>")
            for fi, feed in enumerate(sec["feeds"]):
                fid = self.fid[(si, fi)]
                href = f"{sec['file']}#{fid}"
                n_art = len(feed["articles"])
                src.append(f"<li>▸ <a href=\"{href}\">{_esc(feed['name'])}</a>"
                           f" <span style=\"color:#999\">({n_art} 篇)</span></li>")
            src.append("</ul>")
            toc_entries.append((sec["name"], f"sources.xhtml#top"))
        src.append("</div>")
        sources = epub.EpubHtml(uid="sources", file_name="sources.xhtml",
                                title="今日源目录")
        sources.content = _xhtml("今日源目录", "".join(src))
        book.add_item(style(sources))

        part_items = {}
        for si, sec in enumerate(self.sections):
            file = sec["file"]
            head = [f"<h1 class=\"cat\" id=\"top\">{_esc(sec['name'])}"
                    f" <a class=\"part\" href=\"sources.xhtml#top\">(回源列表)</a></h1>"]
            for fi, feed in enumerate(sec["feeds"]):
                fid = self.fid[(si, fi)]
                head.append(f"<h2 class=\"feed\" id=\"{fid}\">{_esc(feed['name'])}</h2>")
                if feed.get("subtitle"):
                    head.append(f"<p class=\"meta\">{_esc(feed['subtitle'])}</p>")
                for ai, article in enumerate(feed["articles"]):
                    aid = self.aid[(si, fi, ai)]
                    head.append(f"<article id=\"{aid}\">"
                                f"{self._art_inner(si, fi, ai, article)}</article>")
            part = epub.EpubHtml(uid=f"part{si}", file_name=file, title=sec["name"])
            part.content = _xhtml(sec["name"], "".join(head))
            book.add_item(style(part))
            part_items[si] = part

        for uid, fn, mt, blob in self.images:
            book.add_item(epub.EpubImage(uid=uid, file_name=fn, media_type=mt,
                                         content=blob))

        # 设备 TOC:源目录页 + 各分类页(阅读本身依赖连续流与顶部窄条)
        book.toc = [sources] + [part_items[si] for si in range(len(self.sections))]
        book.spine = ["cover", "sources"] + [part_items[si] for si in range(len(self.sections))]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        out_path.parent.mkdir(parents=True, exist_ok=True)
        epub.write_epub(str(out_path), book)
        return out_path

    def _art_inner(self, si, fi, ai, article):
        pos = self._pos(si, fi, ai)
        aid = self.aid[(si, fi, ai)]
        prev_art = self.order[pos - 1] if pos > 0 else None
        next_art = self.order[pos + 1] if pos < len(self.order) - 1 else None
        links = []
        if prev_art:
            ps, pf, pa = prev_art
            links.append(f"<a href=\"{self.sections[ps]['file']}#{self.aid[(ps, pf, pa)]}\">‹ 上篇</a>")
        links.append("<a href=\"sources.xhtml#top\">☰ 目录</a>")
        if next_art:
            ns, nf, na = next_art
            links.append(f"<a href=\"{self.sections[ns]['file']}#{self.aid[(ns, nf, na)]}\">下篇 ›</a>")
        links.append("<a href=\"sources.xhtml#top\">源列表 ↺</a>")
        # 上一源 / 下一源
        nb = self._feed_neighbors(si, fi)
        src_links = []
        if "prev" in nb:
            s, f, name, fid = nb["prev"]
            src_links.append(f"<a href=\"{self.sections[s]['file']}#{fid}\">‹上一源:{name[:14]}</a>")
        if "next" in nb:
            s, f, name, fid = nb["next"]
            src_links.append(f"<a href=\"{self.sections[s]['file']}#{fid}\">下一源:{name[:14]}›</a>")
        nav = '<span class="sep">|</span>'.join(links)
        if src_links:
            nav += ' <span class="src">' + \
                   '<span class="sep">·</span>'.join(src_links) + '</span>'
        meta = [_esc(article.feed_title)]
        if article.pub:
            meta.append(_esc(article.pub))
        if article.link:
            meta.append(f"<a href=\"{_esc(article.link)}\">原文</a>")
        if article.mode == "partial":
            meta.append('<span style="color:#b00">(此篇仅摘要)</span>')
        body = [f"<nav class=\"topbar\">{nav}</nav>",
                f"<h3 class=\"art\">{_esc(article.title)}</h3>",
                f"<p class=\"meta\">{' · '.join(meta)}</p>",
                f"<div class=\"content\">{article.body or '<p>(无正文)</p>'}</div>",
                "<hr class=\"artend\"/>"]
        return "".join(body)


# ---------------------------------------------------------------- 邮箱
def send_email(cfg, subject, attachment_paths, date_str):
    smtp_cfg, mail = cfg.get("smtp", {}), cfg.get("mail", {})
    host = smtp_cfg.get("host") or os.getenv("SMTP_HOST")
    user = smtp_cfg.get("user") or os.getenv("SMTP_USERNAME")
    pwd = smtp_cfg.get("password") or os.getenv("SMTP_PASSWORD")
    to = mail.get("to") or os.getenv("MAIL_TO")
    port = int(smtp_cfg.get("port") or os.getenv("SMTP_PORT") or 465)
    if not (host and user and pwd and to):
        log.warning("缺少 SMTP/Kindle 配置,跳过邮件。")
        return False
    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(f"{date_str} 每日摘要,共 {len(attachment_paths)} 本。", "plain", "utf-8"))
    for p in attachment_paths:
        part = MIMEApplication(p.read_bytes(), _subtype="epub")
        part.add_header("Content-Disposition", "attachment", filename=p.name)
        msg.attach(part)
    try:
        srv = smtplib.SMTP_SSL(host, port, timeout=60) if port == 465 else smtplib.SMTP(host, port, timeout=60)
        if port != 465:
            srv.starttls()
        srv.login(user, pwd)
        srv.sendmail(user, [to], msg.as_string())
        srv.quit()
        log.info("邮件已发送到 %s", to)
        return True
    except Exception as e:
        log.error("邮件发送失败: %s", e)
        return False


# ---------------------------------------------------------------- 主流程
def entry_summary(entry):
    """feed 自带的最佳 HTML(优先 content:encoded,其次 summary)。"""
    if entry.get("content") and entry.content[0].get("value"):
        return entry.content[0]["value"]
    return entry.get("summary") or entry.get("description") or ""


def entry_has_full(entry):
    html = entry_summary(entry)
    n = _plain_len(html)
    return n > 2600 or (bool(entry.get("content")) and n > 700)


def write_progress(path, data):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="每日 RSS → EPUB(连续流)")
    base = Path(__file__).resolve().parent
    ap.add_argument("--opml", default=str(base / "feeds.opml"))
    ap.add_argument("--output", default=str(base / "output"))
    ap.add_argument("--state", default=str(base / "state"))
    ap.add_argument("--history", default=str(base / "history"))
    ap.add_argument("--category", default="")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--max-per-feed", type=int, default=15)
    ap.add_argument("--no-fulltext", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--max-images", type=int, default=600)
    ap.add_argument("--per-article-images", type=int, default=10)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=25)
    ap.add_argument("--send-email", action="store_true")
    ap.add_argument("--refresh", action="store_true",
                    help="忽略去重重跑窗口内内容(用于界面手动重生成)")
    ap.add_argument("--log", default=str(base / "logs" / "rss2epub.log"))
    args = ap.parse_args(argv)

    log_dir = Path(args.log).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(args.log, encoding="utf-8")])

    t0 = time.time()
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    window_start = time.time() - args.hours * 3600
    cats = parse_opml(args.opml)
    log.info("OPML %d 分类 / %d 源", len(cats), sum(len(f) for _, f in cats))

    wanted = [c.strip() for c in args.category.split(",") if c.strip()]
    if wanted:
        cats = [(n, f) for n, f in cats if any(w in n for w in wanted)]

    cfg = {}
    cpath = base / "config.json"
    if cpath.exists():
        try:
            cfg = json.loads(cpath.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("config.json 解析失败:%s", e)
    seen = Seen(Path(args.state) / "seen.txt")
    prog = Path(args.state) / "progress.json"

    sess = requests.Session()
    # 先精确别名(feed_aliases: 官方/替代源),再通用 rsshub_base 换实例
    alias = {a.get("from"): a.get("to") for a in (cfg.get("feed_aliases") or [])
             if a.get("from") and a.get("to")}
    rsshub_base = (cfg.get("rsshub_base") or "").strip().rstrip("/")

    def resolve_feed_url(raw):
        if raw in alias:
            return alias[raw]
        if rsshub_base and raw.startswith("https://rsshub.app/"):
            return rsshub_base + raw[len("https://rsshub.app"):]
        return raw

    feed_plan = [(cat, title, resolve_feed_url(url))
                 for cat, feeds in cats for title, url in feeds]

    # 1) 抓源
    raw = {}
    write_progress(prog, {"running": True, "stage": "fetch", "done": 0,
                          "total": len(feed_plan), "log": []})
    log.info("抓取 %d 源…", len(feed_plan))
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fulltext.fetch_feed, url, args.timeout, sess): (cat, title, url)
                for cat, title, url in feed_plan}
        done = 0
        for fu in as_completed(futs):
            cat, title, url = futs[fu]
            try:
                raw[(cat, title, url)] = fu.result()
            except Exception as e:
                log.warning("抓取失败 [%s] %s → %s", title, url, e)
            done += 1
            if done % 20 == 0:
                write_progress(prog, {"running": True, "stage": "fetch",
                                      "done": done, "total": len(feed_plan)})
    write_progress(prog, {"running": True, "stage": "正文", "done": 0,
                          "total": 0, "log": []})

    # 2) 挑条目
    picked = {}
    summary_by_link, feed_by_link = {}, {}
    for (cat, title, url), entries in raw.items():
        for e in entries:
            link = fulltext.entry_link(e)
            if link and link not in summary_by_link:
                summary_by_link[link] = entry_summary(e)
                feed_by_link[link] = (title, url)
    for (cat, title, url), entries in raw.items():
        count = 0
        for e in entries:
            if args.max_per_feed and count >= args.max_per_feed:
                break
            link = fulltext.entry_link(e)
            if not link or (not args.refresh and seen.contains(link)):
                continue
            ep = fulltext.entry_date_epoch(e)
            if ep is not None and ep < window_start:
                continue
            picked.setdefault(cat, []).append(Article(
                feed_title=title, feed_url=url,
                title=clean_title(e.get("title") or "(无标题)"),
                link=link, pub=e.get("published") or e.get("updated") or "",
                raw_html=summary_by_link.get(link, ""),
                feed_has_full=entry_has_full(e)))
            count += 1
    all_arts = [a for v in picked.values() for a in v]
    log.info("窗口内新条目 %d 篇", len(all_arts))
    if not all_arts:
        write_progress(prog, {"running": False, "stage": "完成", "done": 0,
                              "total": 0, "ok": True, "message": "无新内容"})
        return 0

    # 3) 全文(全运行并发)
    write_progress(prog, {"running": True, "stage": "取全文", "done": 0,
                          "total": len(all_arts)})

    def enrich(a: Article):
        if args.no_fulltext:
            body, mode, detail = a.raw_html, ("feed_full" if a.feed_has_full else "partial"), "跳过正文抓取"
        else:
            body, mode, detail = fulltext.get_body(
                a.link, a.raw_html, a.feed_has_full, cfg, args.timeout)
        a.body = sanitize_html(body)
        a.mode, a.detail = mode, detail
        if a.mode not in FULL_MODES and not strip_tags(a.body):
            a.body = (f"<p>(未取得正文,请<a href=\"{_esc(a.link)}\">打开原文</a>)</p>")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(enrich, a): a for a in all_arts}
        done = 0
        for fu in as_completed(futs):
            a = futs[fu]
            try:
                fu.result()
            except Exception as e:
                a.mode, a.detail = "blocked", "异常"
                log.warning("正文失败 [%s]: %s", a.title, e)
            done += 1
            if done % 25 == 0 or done == len(all_arts):
                write_progress(prog, {"running": True, "stage": "取全文",
                                      "done": done, "total": len(all_arts)})

    # 4) 图片(全运行共池、并发下载;每书只取自己引用的)
    pool = ImagePool(sess, args.timeout, args.per_article_images,
                     0 if args.no_images else args.max_images,
                     cookie_box=fulltext._cookie_box(cfg))

    def _resolve(a):
        a.body = pool.resolve(a.body)
        return a

    if args.max_images > 0:
        with ThreadPoolExecutor(max_workers=max(4, args.workers)) as ex:
            all_arts = list(ex.map(_resolve, all_arts))

    # 5) 组织 sections
    def mk_sections(cat_filter=None):
        sections = []
        for cat, feeds in cats:
            if cat_filter and cat != cat_filter:
                continue
            arts = picked.get(cat, [])
            if not arts:
                continue
            by_feed = {}
            for a in arts:
                by_feed.setdefault(a.feed_title, []).append(a)
            feeds = []
            for a in arts:
                if a.feed_title not in by_feed or any(
                        f["name"] == a.feed_title for f in feeds):
                    continue
                feeds.append({"name": a.feed_title, "subtitle": a.feed_url,
                              "articles": by_feed[a.feed_title]})
            sections.append({"name": cat, "file": f"part{len(sections)}.xhtml",
                             "feeds": feeds})
        return sections

    def imgs_for(articles):
        names = set()
        for a in articles:
            for m in re.finditer(r'src="(images/[^"]+)"', a.body):
                names.add(m.group(1))
        return [b for b in pool.blobs if b[1] in names]

    today_out = Path(args.output) / date_str
    made = []
    for cat, feeds in cats:
        arts = picked.get(cat, [])
        if not arts:
            continue
        secs = mk_sections(cat)
        if not secs:
            continue
        lb = LinearBook(f"{cat} · {date_str}", date_str, secs, imgs_for(arts))
        p = today_out / f"{sanitize_filename(cat)}-{date_str}.epub"
        lb.build(p)
        made.append(p)
        log.info("已生成: %s (%d 篇)", p.name, len(arts))

    combined = None
    if args.send_email:
        allsecs = mk_sections()
        if allsecs:
            lb = LinearBook(f"每日摘要 · {date_str}", date_str, allsecs,
                            imgs_for(all_arts))
            combined = today_out / f"全部-{date_str}.epub"
            lb.build(combined)
            made.append(combined)
            log.info("已生成合并本: %s", combined.name)
        send_email(cfg, f"每日摘要 {date_str}", [combined] if combined else [], date_str)

    # 6) 统计入库(history/日期.json)
    for a in all_arts:
        seen.mark(a.link)
    seen.save()

    report = {"date": date_str, "generated_at": now.isoformat(),
              "total": len(all_arts), "made": [p.name for p in made],
              "categories": []}
    for cat, feeds in cats:
        arts = picked.get(cat, [])
        if not arts:
            continue
        cat_stat = {"name": cat, "n": len(arts), "feeds": []}
        by_feed = {}
        for a in arts:
            by_feed.setdefault(a.feed_title, []).append(a)
        for ft, items in by_feed.items():
            cnt = {"feed": ft, "url": items[0].feed_url, "n": len(items),
                   "full": 0, "partial": 0, "blocked": 0,
                   "articles": []}
            for a in items:
                if a.mode in FULL_MODES:
                    cnt["full"] += 1
                elif a.mode == "partial":
                    cnt["partial"] += 1
                else:
                    cnt["blocked"] += 1
                cnt["articles"].append({"t": a.title, "m": a.mode, "d": a.detail,
                                        "l": a.link, "pub": a.pub})
            cat_stat["feeds"].append(cnt)
        report["categories"].append(cat_stat)

    hist_dir = Path(args.history)
    hist_dir.mkdir(parents=True, exist_ok=True)
    hfile = hist_dir / f"{date_str}.json"
    # 单分类重跑时与当天已有历史合并,避免覆盖其它分类
    if hfile.exists():
        try:
            old = json.loads(hfile.read_text(encoding="utf-8"))
            merged = {c["name"]: c for c in old.get("categories", [])}
            for c in report["categories"]:
                merged[c["name"]] = c
            report["categories"] = list(merged.values())
        except Exception:
            pass
    report["total"] = sum(c["n"] for c in report["categories"])
    hfile.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    write_progress(prog, {"running": False, "stage": "完成", "done": len(all_arts),
                          "total": len(all_arts), "ok": True,
                          "message": f"生成 {len(made)} 本",
                          "report": report})

    log.info("完成,耗时 %.0f 秒 → %s", time.time() - t0, today_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
