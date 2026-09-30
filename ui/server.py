#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEVTUO-RWP —— 统一 Web 界面服务端。

A. CEVTUO合集坊(对标 EpubKit):多个网址 / 网站内链接 / RSS / Markdown 导入,
   章节管理、逐页查看与编辑、导出 EPUB(无 10 页限制)
B. Rssdailyepub: RSS 源管理、一键生成、进度、定时

启动: ./.venv/bin/python ui/server.py [--port 8611] [--open]
"""
import argparse
import base64
import glob
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CODE = Path(__file__).resolve().parent.parent     # 源码(可能只读)
UI_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE))

import capture          # noqa: E402
import coverart         # noqa: E402
import export_epub      # noqa: E402
import extract_page     # noqa: E402
import imports          # noqa: E402
import jobs             # noqa: E402
import opml             # noqa: E402
import scheduler        # noqa: E402
import store            # noqa: E402
import paths            # noqa: E402

BASE = paths.ensure()                     # 数据目录(可写):DB/输出/日志/历史

OPML_PATH = paths.seed("feeds.opml")      # 首次运行从程序自带的那份播种过来
PORT = 8611

# 软件自己的版本号。
# ⚠️ 以前设置页那行「版本」显示的是 capture.status()["version"],那是 **Chrome 的版本**,
# 不是本软件的。label 写的确实是「浏览器版本」,但用户看到的就一个版本号,
# 会当成软件版本 —— 加这一行区分开。
APP_VERSION = "1.3"

# ⚠️ 这个名字不能叫 log —— 下面几行就有一个 def log(*a)。
# 叫 log 的话会被那个函数覆盖,而 AttributeError 出现在两个很难查的地方:
#   · _delete_book 结尾 —— 文件**已经删掉了**才抛,于是接口回报失败、文件其实没了
#   · _write_index 里   —— 异常被 except 吞掉,索引永远写不进去且完全没提示
slog = logging.getLogger("server")


def log(*a):
    print(*a, flush=True)


def sanitize_filename(name, fallback="book"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip().rstrip(".")
    return name or fallback


# ------------------------------------------------------------------ 工具 -----
def json_resp(h, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def read_body(h):
    n = int(h.headers.get("Content-Length") or 0)
    if not n:
        return {}
    raw = h.rfile.read(n)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def tail(path, n=60):
    try:
        return Path(path).read_text(encoding="utf-8",
                                    errors="ignore").splitlines()[-n:]
    except Exception:
        return []


def file_info(p: Path):
    st = p.stat()
    return {"name": p.name, "path": str(p), "size": st.st_size,
            "mtime": st.st_mtime,
            "mtime_str": time.strftime("%Y-%m-%d %H:%M",
                                       time.localtime(st.st_mtime))}


def snapshot(d: Path):
    """目录里每个 epub 的 (name -> (mtime, size)),用来算"这次新生成了哪些"。"""
    out = {}
    if d.exists():
        for p in d.glob("*.epub"):
            try:
                st = p.stat()
                out[p.name] = (st.st_mtime, st.st_size)
            except Exception:
                pass
    return out


# ------------------------------------------------------------------ 书库 -----
# 合集电子书**平铺**在 books/ 下,不再一个合集一个文件夹。
# 原来 20 个合集就是 20 个各装一两本书的文件夹,在访达里翻起来很烦。
#
# 平铺之后「这个 epub 属于哪个合集」不能靠文件名猜 —— 合集标题可以互为前缀
# (「科技」和「科技日报」),sanitize 还会把 / : 换掉。所以用一个**点开头**的
# 索引文件记录归属。macOS 访达默认不显示点文件,用户只会看到一堆 epub。
BOOK_INDEX = ".cevtuo-index.json"


def _index_path():
    return store.books_dir() / BOOK_INDEX


def _read_index():
    try:
        d = json.loads(_index_path().read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_index(d):
    try:
        p = _index_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    except Exception as e:
        slog.warning("写书库索引失败: %s", e)


def _register_book(path: Path, cid):
    d = _read_index()
    d[path.name] = {"cid": cid, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _write_index(d)


def _unique_path(d: Path, name: str) -> Path:
    """同名就加序号 —— 平铺之后两个同名合集在同一分钟导出会撞名。"""
    p = d / name
    if not p.exists():
        return p
    stem, suf = p.stem, p.suffix
    for i in range(2, 100):
        p = d / f"{stem}-{i}{suf}"
        if not p.exists():
            return p
    return d / f"{stem}-{int(time.time())}{suf}"


def _books_of_collection(cid):
    """这个合集生成过的所有 epub。

    ⚠️ 只认**索引里明确记着属于它**的,以及老结构 books/<id>/ 里的。
    不做「按标题前缀认」—— 合集标题互为前缀时(「科技」/「科技日报」)
    那会认到隔壁合集的头上。
    """
    bdir = store.books_dir()
    out = []
    idx = _read_index()
    for name, rec in idx.items():
        if isinstance(rec, dict) and rec.get("cid") == cid:
            p = bdir / name
            if p.exists():
                out.append(p)
    d = bdir / cid                       # 老结构
    if d.is_dir():
        out += sorted(d.glob("*.epub"))
    return out


def _delete_books_of(cid):
    """删合集时把它生成过的 epub 一起删掉。"""
    bdir = store.books_dir()
    gone = []
    for p in _books_of_collection(cid):
        try:
            p.unlink()
            gone.append(str(p))
        except Exception as e:
            slog.warning("删书失败 %s: %s", p, e)
    idx = _read_index()
    for name in [Path(g).name for g in gone]:
        idx.pop(name, None)
    _write_index(idx)
    d = bdir / cid
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    return gone


def _forget_index(names):
    idx = _read_index()
    for n in names:
        idx.pop(n, None)
    _write_index(idx)


def library():
    """书库:合集书(平铺)+ Rssdailyepub 生成的书(按日期成文件夹)。"""
    bdir = store.books_dir()
    colls = {c["id"]: c for c in store.list_collections()}
    for c in colls.values():
        c["books"] = []
    idx = _read_index()
    strip = {cid: sanitize_filename(c["title"]) for cid, c in colls.items()}
    by_prefix = sorted(colls.values(), key=lambda c: -len(strip[c["id"]]))
    orphan = []

    def owner(p: Path):
        rec = idx.get(p.name)
        if isinstance(rec, dict) and rec.get("cid") in colls:
            return colls[rec["cid"]]
        # 索引对不上(用户手动搬过文件、或老版本留下的)—— 退回按标题前缀认
        for c in by_prefix:
            t = strip[c["id"]]
            if t and p.name.startswith(t + "-"):
                return c
        return None

    if bdir.exists():
        for p in sorted(bdir.glob("*.epub"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            c = owner(p)
            (c["books"] if c else orphan).append(file_info(p))
        for c in colls.values():         # 老结构:books/<合集id>/*.epub,保留可见
            d = bdir / c["id"]
            if d.is_dir():
                c["books"] += [file_info(p) for p in sorted(
                    d.glob("*.epub"), key=lambda p: p.stat().st_mtime,
                    reverse=True)]

    rss = []
    out = store.output_dir()
    if out.exists():
        for day in sorted([d for d in out.iterdir() if d.is_dir()],
                          key=lambda d: d.name, reverse=True):
            books = [file_info(p) for p in sorted(day.glob("*.epub"))]
            if not books:
                continue
            rss.append({"date": day.name, "path": str(day),
                        "count": len(books), "books": books,
                        "total_size": sum(b["size"] for b in books)})
    return {"collections": list(colls.values()), "rss": rss, "orphan": orphan,
            "books_dir": str(bdir), "output_dir": str(out),
            "lib_dir": str(store.lib_dir())}


def set_library_dir(new_path):
    """改书库目录,并把已生成的书一起搬过去(不覆盖同名文件)。"""
    old = store.lib_dir()
    raw = (new_path or "").strip()
    new = Path(raw).expanduser() if raw else BASE
    if not new.is_absolute():
        new = BASE / new
    try:
        new.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise RuntimeError(f"无法创建目录 {new}: {e}")
    moved, skipped = 0, 0
    if old.resolve() != new.resolve():
        for sub in ("output", "books"):
            src = old / sub
            if not src.exists():
                continue
            dst = new / sub
            dst.mkdir(parents=True, exist_ok=True)
            for item in list(src.iterdir()):
                target = dst / item.name
                if target.exists():
                    skipped += 1
                    continue
                try:
                    shutil.move(str(item), str(target))
                    moved += 1
                except Exception as e:
                    log(f"移动失败 {item}: {e}")
            try:
                if src.exists() and not any(src.iterdir()):
                    src.rmdir()
            except Exception:
                pass
    store.set_settings({"library_dir": "" if new == BASE else str(new)})
    return {"lib_dir": str(new), "moved": moved, "skipped": skipped}


def rss_report(date):
    p = BASE / "history" / f"{date}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ------------------------------------------------------------- 导出 epub -----
def do_export(ctx, cid, opts):
    coll = store.get_collection(cid)
    if not coll:
        raise RuntimeError("collection-not-found:合集不存在")
    arts = store.list_articles(cid, with_body=True)
    if not arts:
        raise RuntimeError("empty-collection:合集里还没有文章")
    # 平铺:直接放 books/ 下,不再给每个合集建子文件夹
    d = store.books_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{sanitize_filename(coll['title'])}-{time.strftime('%Y%m%d-%H%M')}.epub"
    out = _unique_path(d, name)
    ctx.log(f"导出「{coll['title']}」(共 {len(arts)} 章)")
    ctx.set_total(len(arts))

    def prog(done, total, msg=None):
        if msg:
            ctx.progress(done, total, msg)
        else:
            ctx.progress(done, total)

    rep = export_epub.build(coll, arts, out, opts, on_progress=prog) or {}
    _register_book(out, cid)             # 平铺之后靠索引认归属
    return {"path": str(out), "name": out.name, "chapters": len(arts),
            "size": out.stat().st_size,
            # 图片账单:界面要显示「下了多少张 / 哪些没下到」,并给重试按钮
            "images": {"wanted": rep.get("wanted", 0), "ok": rep.get("ok", 0),
                       "failed": rep.get("failed", [])},
            "collectionId": cid}


# ------------------------------------------------------------ RSS 生成 -------
def rss_generate_job(ctx, payload):
    # 用 sys.executable —— 打包形态下是自带的 Python,开发形态下是 .venv 的
    args = [sys.executable, str(CODE / "rss2epub.py"),
            "--opml", str(OPML_PATH),
            "--output", str(store.output_dir()),
            "--state", str(BASE / "state"),
            "--history", str(BASE / "history"),
            "--log", str(BASE / "logs" / "ui_run.log")]
    hours = int(payload.get("hours") or 24)
    args += ["--hours", str(hours)]
    if payload.get("maxPerFeed"):
        args += ["--max-per-feed", str(int(payload["maxPerFeed"]))]
    cats = [c for c in (payload.get("categories") or []) if c]
    if cats:
        args += ["--category", ",".join(cats)]
        ctx.log(f"只做这些分类: {'、'.join(cats)}")
    else:
        ctx.log("未指定分类 → 做全部分类")
    if payload.get("refresh"):
        args.append("--refresh")
    if payload.get("sendEmail"):
        args.append("--send-email")
    if payload.get("noImages"):
        args.append("--no-images")

    out_dir = store.output_dir() / time.strftime("%Y-%m-%d")
    before = snapshot(out_dir)          # 关键:只把"这次新生成/更新"的报给用户
    prog = BASE / "state" / "progress.json"
    proc = subprocess.Popen(args, cwd=str(CODE), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    last = ""
    while proc.poll() is None:
        if ctx.should_stop():
            proc.terminate()
            break
        try:
            d = json.loads(prog.read_text(encoding="utf-8"))
            msg = f"{d.get('stage', '')} {d.get('done', 0)}/{d.get('total', 0)}"
            if msg.strip() != last:
                last = msg.strip()
                ctx.progress(d.get("done", 0), d.get("total", 0), msg)
        except Exception:
            pass
        time.sleep(1.0)
    out = (proc.stdout.read() if proc.stdout else "") or ""
    for line in [l for l in out.strip().splitlines() if l.strip()][-3:]:
        ctx.log(line)

    after = snapshot(out_dir)
    made = [n for n, v in after.items() if before.get(n) != v]
    if not made:
        ctx.log("这次没有新内容(窗口内无更新,或都已在去重库里)")
    return {"returncode": proc.returncode, "made": sorted(made),
            "date": time.strftime("%Y-%m-%d"),
            "output_dir": str(out_dir)}


# ------------------------------------------------------------- 在线封面 -----
# 三个都不需要 API Key,互相当备份 —— 单个源经常坏或者查不到中文书。
COVER_UA = {"User-Agent": "CEVTUO-RWP/1.1 (cover search)"}


def _cover_from_google(q, n=8):
    import requests
    r = requests.get("https://www.googleapis.com/books/v1/volumes",
                     params={"q": q, "maxResults": n}, headers=COVER_UA,
                     timeout=12)
    r.raise_for_status()
    out = []
    for it in (r.json().get("items") or []):
        vi = it.get("volumeInfo") or {}
        im = vi.get("imageLinks") or {}
        u = im.get("thumbnail") or im.get("smallThumbnail")
        if not u:
            continue
        u = u.replace("http://", "https://")          # 页面是 https,混用会被拦
        out.append({"url": u.replace("&zoom=1", "&zoom=2"),
                    "thumb": u, "title": vi.get("title") or "",
                    "source": "Google Books"})
    return out


def _cover_from_openlibrary(q, n=8):
    import requests
    r = requests.get("https://openlibrary.org/search.json",
                     params={"q": q, "limit": n,
                             "fields": "title,cover_i,author_name"},
                     headers=COVER_UA, timeout=12)
    r.raise_for_status()
    out = []
    for d in (r.json().get("docs") or []):
        ci = d.get("cover_i")
        if not ci:
            continue
        out.append({"url": f"https://covers.openlibrary.org/b/id/{ci}-L.jpg",
                    "thumb": f"https://covers.openlibrary.org/b/id/{ci}-M.jpg",
                    "title": d.get("title") or "", "source": "Open Library"})
    return out


def _cover_from_commons(q, n=8):
    import requests
    r = requests.get("https://commons.wikimedia.org/w/api.php",
                     params={"action": "query", "format": "json",
                             "generator": "search", "gsrsearch": q,
                             "gsrnamespace": "6", "gsrlimit": n,
                             "prop": "imageinfo", "iiprop": "url",
                             "iiurlwidth": "600"},
                     headers=COVER_UA, timeout=12)
    r.raise_for_status()
    pages = ((r.json().get("query") or {}).get("pages") or {})
    out = []
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        u = ii.get("thumburl") or ii.get("url")
        if not u or not re.search(r"\.(jpe?g|png|webp)$",
                                  u.split("?")[0], re.I):
            continue
        out.append({"url": u, "thumb": u, "source": "Wikimedia",
                    "title": (p.get("title") or "").replace("File:", "")})
    return out


def _cover_from_openverse(q, n=8):
    """Openverse —— 免费、不需要 key 的**通用图片**搜索。

    ⚠️ 前三个源都是**书**的 API。查「Ft-093026」这种自己起的合集名一无所获,
    而 Google Books 还会直接 429。用户搜封面想要的是「给我几张像样的图」,
    所以必须有一个不限于书的通用源。
    """
    import requests
    r = requests.get("https://api.openverse.org/v1/images/",
                     params={"q": q, "page_size": n}, headers=COVER_UA,
                     timeout=12)
    r.raise_for_status()
    out = []
    for it in (r.json().get("results") or []):
        thumb = it.get("thumbnail") or it.get("url")
        if not thumb:
            continue
        out.append({"url": it.get("url") or thumb, "thumb": thumb,
                    "title": (it.get("title") or "")[:80],
                    "source": "Openverse"})
    return out


def _cover_from_wikipedia(q, n=6):
    """维基百科条目的代表图 —— **对任意关键词最有用的一条**。

    ⚠️ 前面几个源都是「书」的库:查得到《Sapiens》,查不到「东京 街道 夜景」,
    更查不到用户自拟的合集名(实测中文查询四个源全部返回 0)。
    维基是按**条目**匹配的,中英双语都覆盖,泛化能力最强。
    """
    import requests
    out = []
    for host in ("zh.wikipedia.org", "en.wikipedia.org"):
        try:
            r = requests.get(f"https://{host}/w/api.php",
                             params={"action": "query", "format": "json",
                                     "generator": "search", "gsrsearch": q,
                                     "gsrlimit": n, "prop": "pageimages",
                                     "pithumbsize": "800"},
                             headers=COVER_UA, timeout=10)
            r.raise_for_status()
            pages = ((r.json().get("query") or {}).get("pages") or {})
        except Exception:
            continue
        for p in pages.values():
            t = (p.get("thumbnail") or {}).get("source")
            if not t:
                continue
            out.append({"url": t, "thumb": t, "source": "Wikipedia",
                        "title": p.get("title") or ""})
    return out


# ⚠️ Google Books 放在**最后**:从本机这个出口 IP 访问它几乎每次都是 429,
#    放前面只会白等 12 秒超时,而且它一失败就会在错误列表里占第一位,
#    把「其它源真的没搜到」这件事盖过去。
COVER_SOURCES = (_cover_from_wikipedia, _cover_from_openverse,
                 _cover_from_openlibrary, _cover_from_commons,
                 _cover_from_google)


def search_covers(q, per_source=8):
    """四个源都试一遍,合并去重。

    返回 (results, errs)。**errs 单独回** —— 「一个都没搜到」和
    「源全挂了」是两件事:前者换个词就行,后者换词也没用。
    """
    results, errs = [], []
    for fn in COVER_SOURCES:
        try:
            results += fn(q, per_source)
        except Exception as e:
            errs.append(f"{fn.__name__.replace('_cover_from_', '')}: "
                        f"{str(e)[:60]}")
    seen, uniq = set(), []
    for it in results:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq, errs


# ------------------------------------------------------------------ API ------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _serve_static(self):
        up = urlparse(self.path)
        rel = "index.html" if up.path in ("/", "/index.html") else up.path.lstrip("/")
        f = (UI_DIR / rel).resolve()
        if not str(f).startswith(str(UI_DIR.resolve())) or not f.exists():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(f.name)[0] or "text/html"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        up = urlparse(self.path)
        r = up.path
        qs = parse_qs(up.query)
        try:
            if r == "/api/state":
                json_resp(self, {
                    "settings": store.get_settings(),
                    "capture": capture.status(),
                    "schedule": scheduler.status(),
                    "jobs": jobs.recent_activity(),
                    "paths": {"lib_dir": str(store.lib_dir()),
                              "output_dir": str(store.output_dir()),
                              "books_dir": str(store.books_dir()),
                              "base": str(BASE), "opml": str(OPML_PATH)},
                    "app": "CEVTUO-RWP",
                    "app_version": APP_VERSION,
                })
            elif r == "/api/collections":
                json_resp(self, {"collections": store.list_collections()})
            elif r == "/api/collection":
                c = store.get_collection(qs.get("id", [""])[0])
                json_resp(self, c or {"err": "not found"}, 200 if c else 404)
            elif r == "/api/articles":
                json_resp(self, {"articles": store.list_articles(
                    qs.get("collection", [""])[0])})
            elif r == "/api/article":
                a = store.get_article(qs.get("id", [""])[0])
                json_resp(self, a or {"err": "not found"}, 200 if a else 404)
            elif r == "/api/library":
                json_resp(self, library())
            elif r == "/api/cover":
                self._serve_cover(qs.get("id", [""])[0])
            elif r == "/api/rss/sources":
                cats = opml.read(OPML_PATH)
                json_resp(self, {
                    "categories": [{"name": c["name"], "count": len(c["feeds"]),
                                    "feeds": c["feeds"]} for c in cats],
                    "total": sum(len(c["feeds"]) for c in cats),
                    "opml": str(OPML_PATH)})
            elif r == "/api/rss/dates":
                files = sorted(glob.glob(str(BASE / "history" / "*.json")),
                               reverse=True)
                json_resp(self, {"dates": [Path(f).stem for f in files]})
            elif r == "/api/rss/report":
                d = qs.get("date", [""])[0]
                rep = rss_report(d) if d else None
                json_resp(self, rep or {"err": "not found"}, 200 if rep else 404)
            elif r == "/api/rss/schedule":
                json_resp(self, scheduler.status())
            elif r == "/api/jobs":
                json_resp(self, {"jobs": jobs.recent_activity(20)})
            elif r == "/api/job":
                j = jobs.get(qs.get("id", [""])[0])
                json_resp(self, j or {"err": "not found"}, 200 if j else 404)
            elif r == "/api/logtail":
                n = int(qs.get("n", ["60"])[0])
                which = qs.get("which", ["ui_run"])[0]
                json_resp(self, {"lines": tail(BASE / "logs" / f"{which}.log", n)})
            elif r == "/api/capture/status":
                json_resp(self, capture.status())
            elif r == "/api/fs/list":
                json_resp(self, self._fs_list(qs.get("path", ["~"])[0]))
            elif r == "/api/open":
                self._open_path(qs)
            else:
                self._serve_static()
        except Exception as e:
            json_resp(self, {"err": str(e)}, 500)

    def _delete_group(self, b):
        """把**一整组**书删掉 —— 书库卡片上那个 ✕。

        ⚠️ 只删生成出来的 epub,**不删合集本身、也不删里面的文章**。
        用户说的是「把这个框也删掉,因为内容也删掉了」—— 框之所以消失,
        正是因为里面一本书都不剩了(界面只显示有书的合集)。
        文章还在,随时能重新导出;连文章一起抹掉就找不回来了。
        """
        kind = (b.get("kind") or "").strip()
        key = (b.get("key") or "").strip()
        if not key:
            return {"ok": False, "err": "缺少参数"}
        if kind == "collection":
            if not store.get_collection(key):
                return {"ok": False, "err": "合集不存在"}
            targets = _books_of_collection(key)
        elif kind == "date":
            d = store.output_dir() / key
            if not d.is_dir():
                return {"ok": False, "err": "这一天的书已经不在了"}
            targets = sorted(d.glob("*.epub"))
        else:
            return {"ok": False, "err": "未知的分组类型"}

        removed, failed = [], []
        for p in targets:
            try:
                p.unlink()
                removed.append(str(p))
            except Exception as e:
                failed.append({"path": str(p), "why": str(e)[:80]})
        if kind == "collection":
            _forget_index([Path(x).name for x in removed])
        else:
            # 日期文件夹空了就一起收掉,否则库里会留一堆空文件夹,
            # 而它们在外观上和"有书的"一模一样。
            d = store.output_dir() / key
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except Exception as e:
                slog.warning("清空文件夹失败 %s: %s", d, e)
        slog.info("整组删除 %s/%s:成功 %d,失败 %d",
                  kind, key, len(removed), len(failed))
        return {"ok": True, "removed": removed, "failed": failed,
                "count": len(removed)}

    def _delete_book(self, b):
        """删一本书,连同磁盘上的文件。

        ⚠️ 必须先确认这个路径真的落在书库/输出目录**里面** ——
        否则一个 POST 就能删掉用户磁盘上任意一个文件(../../ 之类)。
        resolve() 之后再比,符号链接和 .. 都挡得住。
        """
        raw = (b.get("path") or "").strip()
        if not raw:
            return {"ok": False, "err": "缺少路径"}
        try:
            p = Path(raw).expanduser().resolve()
        except Exception as e:
            return {"ok": False, "err": f"路径无效: {e}"}
        roots = []
        for r in (store.books_dir(), store.output_dir()):
            try:
                roots.append(r.resolve())
            except Exception:
                pass
        if not any(p == r or r in p.parents for r in roots):
            return {"ok": False, "err": "只能删除书库目录里的文件"}
        if p.suffix.lower() != ".epub":
            return {"ok": False, "err": "只能删除 .epub 文件"}
        if not p.exists():
            return {"ok": False, "err": "文件已经不在了,刷新看看"}
        try:
            p.unlink()
        except Exception as e:
            return {"ok": False, "err": f"删除失败: {e}"}
        idx = _read_index()
        if idx.pop(p.name, None) is not None:
            _write_index(idx)
        slog.info("已删除书籍 %s", p)
        return {"ok": True, "removed": str(p)}

    def _fs_list(self, path):
        """给"选择文件夹"用的简易目录浏览。"""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path.home() / p
        if p.is_file():
            p = p.parent
        if not p.exists():
            p = Path.home()
        dirs = []
        try:
            for d in sorted(p.iterdir()):
                if d.name.startswith(".") or not d.is_dir():
                    continue
                if d.is_symlink() and not d.exists():
                    continue
                dirs.append({"name": d.name, "path": str(d)})
        except PermissionError:
            pass
        return {"path": str(p), "parent": str(p.parent), "dirs": dirs[:300],
                "home": str(Path.home()),
                "suggestions": [
                    {"name": "文稿", "path": str(Path.home() / "Documents")},
                    {"name": "下载", "path": str(Path.home() / "Downloads")},
                    {"name": "桌面", "path": str(Path.home() / "Desktop")},
                    {"name": "iCloud 云盘",
                     "path": str(Path.home() / "Library/Mobile Documents/"
                                 "com~apple~CloudDocs")},
                ]}

    def _open_path(self, qs):
        target = qs.get("target", [""])[0]
        read = qs.get("read", ["0"])[0] == "1"
        if not target or not os.path.exists(target):
            json_resp(self, {"ok": False, "err": "路径不存在"})
            return
        if os.path.isfile(target):
            subprocess.Popen(["open", target] if read else ["open", "-R", target])
        else:
            subprocess.Popen(["open", target])
        json_resp(self, {"ok": True, "target": target})

    def do_POST(self):
        up = urlparse(self.path)
        try:
            b = read_body(self)
            out = self._route_post(up.path, b)
            json_resp(self, out if out is not None else {"ok": True})
        except Exception as e:
            json_resp(self, {"ok": False, "err": str(e)}, 400)

    def _route_post(self, r, b):
        # ---------------------------------------------------- 合集 ----
        if r == "/api/collection/create":
            return store.create_collection(b.get("title"), b.get("author"),
                                           b.get("language_code") or "zh-CN")
        if r == "/api/collection/save":
            return store.update_collection(
                b["id"], title=b.get("title"), author=b.get("author"),
                language_code=b.get("language_code"))
        if r == "/api/collection/delete":
            removed = _delete_books_of(b["id"])
            store.delete_collection(b["id"])
            return {"ok": True, "removed": removed}
        if r == "/api/collection/cover":
            return self._save_cover(b)
        if r == "/api/cover/search":
            return self._cover_search(b)
        if r == "/api/collection/coverUrl":
            return self._save_cover_url(b)
        if r == "/api/cover/fillMissing":
            return self._fill_missing_covers(b)
        if r == "/api/cover/fromUrl":
            return self._cover_from_url(b)
        if r == "/api/cover/reset":
            # 把封面清掉 ⇒ 下次 /api/cover 会按「只有书名和作者」重新生成
            c = store.get_collection(b.get("id") or "")
            if not c:
                return {"ok": False, "err": "合集不存在"}
            d = store.lib_dir() / "covers"
            for old in d.glob(f"{c['id']}.*"):
                try:
                    old.unlink()
                except Exception:
                    pass
            store.update_collection(c["id"], cover="")
            return {"ok": True}

        # ---------------------------------------------------- 章节 ----
        if r == "/api/article/save":
            return store.update_article(b["id"], title=b.get("title"),
                                        parsed_html=b.get("parsed_html"))
        if r == "/api/article/delete":
            store.delete_article(b["id"])
            return {"ok": True}
        if r == "/api/article/move":
            store.move_article(b["id"], int(b.get("delta") or 0))
            return {"articles": store.list_articles(b.get("collectionId") or "")}
        if r == "/api/article/reorder":
            store.reorder(b["collectionId"], b.get("ids"))
            return {"ok": True}
        if r == "/api/article/add":
            return store.add_article(b["collectionId"], b.get("title") or "新页面",
                                     b.get("parsed_html") or "", source="manual",
                                     mode="manual")
        if r == "/api/article/addUrl":
            art, why = imports.article_from_url(
                b["url"], use_chrome=b.get("useChrome", True))
            if not art:
                return {"ok": False, "err": why}
            a = store.add_article(b["collectionId"], art["title"], art["content"],
                                  url=b["url"], source="manual",
                                  mode=art["mode"], note=art.get("note") or "")
            return {"ok": True, "article": a}

        # 「再次获取」—— 重新抓这一章的原文。任何一章都能点,不限于失败的。
        if r == "/api/article/refetch":
            a = store.get_article(b["id"])
            if not a:
                return {"ok": False, "err": "章节不存在"}
            url = (b.get("url") or a.get("url") or "").strip()
            if not url:
                return {"ok": False, "err": "这一章没有原始网址,没法重新获取"}
            art, why = imports.article_from_url(
                url, use_chrome=b.get("useChrome", True))
            if not art:
                # ⚠️ 失败也要把原因写回去。只回一个 err 的话,弹窗关掉之后
                # 那一行长得和上次一模一样,用户分不清是没跑还是又失败了。
                store.update_article(a["id"], note=why)
                return {"ok": False, "err": why,
                        "article": store.get_article(a["id"])}
            store.update_article(a["id"], title=art["title"],
                                 parsed_html=art["content"], url=url,
                                 mode=art["mode"], note=art.get("note") or "")
            return {"ok": True, "article": store.get_article(a["id"]),
                    "chars": art["text_len"], "via": art["via"]}

        # 删一本书(书库右键)。同时把本地文件删掉。
        if r == "/api/book/delete":
            return self._delete_book(b)
        # 删一整组(书库卡片上的 ✕,不管里面有几本)
        if r == "/api/library/deleteGroup":
            return self._delete_group(b)

        # ---------------------------------------------------- 导入 ----
        if r == "/api/import/urls":
            cid = b["collectionId"]
            urls = b.get("urls") or []
            use_chrome = b.get("useChrome", True)
            return {"job": jobs.start("import", f"导入 {len(urls)} 个网址",
                                      lambda ctx: imports.import_urls(
                                          ctx, cid, urls, use_chrome))["id"]}
        if r == "/api/import/website/list":
            links = imports.website_links(
                b["url"], use_chrome=b.get("useChrome", True),
                same_domain=b.get("sameDomain", True),
                mode=b.get("mode") or "links")
            return {"links": links, "count": len(links)}
        if r == "/api/import/website/commit":
            cid = b["collectionId"]
            links = b.get("links") or []
            use_chrome = b.get("useChrome", True)
            return {"job": jobs.start("import", f"导入网站内链接 {len(links)} 页",
                                      lambda ctx: imports.import_links(
                                          ctx, cid, links, use_chrome))["id"]}
        if r == "/api/import/rss/preview":
            items = imports.rss_preview(b["url"], int(b.get("limit") or 40))
            return {"items": items, "count": len(items)}
        if r == "/api/import/rss/commit":
            cid = b["collectionId"]
            items = b.get("items") or []
            use_chrome = b.get("useChrome", True)
            return {"job": jobs.start("import", f"导入 RSS {len(items)} 条",
                                      lambda ctx: imports.import_rss_items(
                                          ctx, cid, items, use_chrome))["id"]}
        if r == "/api/import/markdown":
            cid = b["collectionId"]
            return {"job": jobs.start("import", "导入 Markdown",
                                      lambda ctx: imports.import_markdown(
                                          ctx, cid, b.get("text") or ""))["id"]}

        # ---------------------------------------------------- 导出 ----
        if r == "/api/export":
            cid = b["collectionId"]
            opts = {"remove_preface": bool(b.get("removePreface")),
                    "embed_images": b.get("embedImages", True),
                    "max_images": int(b.get("maxImages") or 800)}
            return {"job": jobs.start(
                "export", "导出 EPUB",
                lambda ctx: do_export(ctx, cid, opts))["id"]}

        # ------------------------------------------------ Rssdailyepub --
        if r == "/api/rss/feed/add":
            opml.add_feed(OPML_PATH, b.get("category") or "未分类",
                          b.get("title"), b["url"])
            return {"ok": True}
        if r == "/api/rss/feed/update":
            opml.update_feed(OPML_PATH, b["url"], new_url=b.get("newUrl"),
                             new_title=b.get("title"),
                             new_category=b.get("category"))
            return {"ok": True}
        if r == "/api/rss/feed/delete":
            opml.delete_feed(OPML_PATH, b["url"])
            return {"ok": True}
        if r == "/api/rss/category/add":
            opml.add_category(OPML_PATH, b["name"])
            return {"ok": True}
        if r == "/api/rss/category/rename":
            opml.rename_category(OPML_PATH, b["name"], b["newName"])
            return {"ok": True}
        if r == "/api/rss/category/delete":
            opml.delete_category(OPML_PATH, b["name"])
            return {"ok": True}
        if r == "/api/rss/import/opml":
            src = Path(b["path"]).expanduser()
            if not src.exists():
                return {"ok": False, "err": "文件不存在"}
            incoming = opml.read(src)
            cur = opml.read(OPML_PATH)
            have = {f["url"] for c in cur for f in c["feeds"]}
            added = 0
            for c in incoming:
                for f in c["feeds"]:
                    if f["url"] in have:
                        continue
                    opml.add_feed(OPML_PATH, c["name"], f["title"], f["url"])
                    have.add(f["url"])
                    added += 1
            return {"ok": True, "added": added}
        if r == "/api/rss/generate":
            return {"job": jobs.start("rss", "生成每日书",
                                      lambda ctx: rss_generate_job(ctx, b))["id"]}
        if r == "/api/rss/settings/save":
            s = store.set_settings({
                "window_days": int(b.get("window_days") or 1),
                "max_per_feed": int(b.get("max_per_feed") or 15),
                "schedule_categories": b.get("schedule_categories") or [],
                "send_email": bool(b.get("send_email")),
            })
            return {"ok": True, "settings": s}
        if r == "/api/rss/schedule/save":
            patch = {"schedule_enabled": bool(b.get("enabled")),
                     "schedule_mode": b.get("mode") or "daily"}
            # 一天里的多个询问时刻,界面传 times 数组。
            # ⚠️ 老的单 hour/minute 也得认 —— 只改前端不改这里的话,
            #    界面上那两个输入框改了完全没用(存进去的字段后端已经不读了),
            #    而页面上不会有任何报错。
            ts = b.get("times")
            clean = []
            if isinstance(ts, list):
                for t in ts:
                    try:
                        h, m = int(t.get("hour")), int(t.get("minute", 0) or 0)
                    except Exception:
                        continue
                    if 0 <= h <= 23 and 0 <= m <= 59:
                        clean.append({"hour": h, "minute": m})
            if not clean and b.get("hour") is not None:
                clean = [{"hour": int(b.get("hour") or 9),
                          "minute": int(b.get("minute") or 0)}]
            if clean:
                patch["schedule_times"] = clean
            store.set_settings(patch)
            return scheduler.apply()
        if r == "/api/rss/force_run":
            st = store.get_settings()
            return {"job": jobs.start("rss", "按定时设置跑一次",
                                      lambda ctx: rss_generate_job(ctx, {
                                          "hours": int(st.get("window_days") or 1) * 24,
                                          "maxPerFeed": st.get("max_per_feed"),
                                          "categories": st.get("schedule_categories") or [],
                                          "sendEmail": st.get("send_email"),
                                      }))["id"]}

        # ---------------------------------------------------- 设置 ----
        if r == "/api/settings/library":
            return set_library_dir(b.get("path") or "")
        if r == "/api/settings/ui":
            cur = store.get_settings().get("ui") or {}
            cur.update({k: v for k, v in (b or {}).items()
                        if k in ("theme", "glass", "lang")})
            store.set_settings({"ui": cur})
            return {"ok": True, "ui": cur}
        if r == "/api/settings/save":
            return {"settings": store.set_settings(b)}

        # -------------------------------------------------- 浏览器 ----
        if r == "/api/capture/sync":
            return {"ok": capture.refresh_profile(), "status": capture.status()}
        if r == "/api/capture/ensure":
            ok, err = capture.ensure(int(store.get_settings().get(
                "chrome_port") or 9222))
            return {"ok": ok, "err": err, "status": capture.status()}
        if r == "/api/capture/test":
            html, err = capture.render(b.get("url") or "https://example.com",
                                       timeout=45)
            return {"ok": bool(html), "err": err, "len": len(html or ""),
                    "title": extract_page.guess_title(html or "")}
        if r == "/api/job/cancel":
            return {"ok": jobs.cancel(b.get("id"))}

        return {"ok": False, "err": f"未知接口 {r}"}

    def _serve_cover(self, cid):
        """按合集 id 把封面图发出去。**没有封面就现场生成一张。**

        ⚠️ 必须走 HTTP,不能用 file://。界面是从 http://127.0.0.1:8611 加载的,
        浏览器会把 http 页面里的 file:// 子资源**直接拦掉**。
        表现就是「封面上传成功了、但显示不出来」—— 只有真的打开界面才看得见,
        接口层面全是 200。

        ⚠️ 没有封面时不再 404,而是生成一张「只有书名和作者」的图。
        用户的要求:「如果没有封面,那就是你只写书名和作者」。
        界面上永远有个东西可看,比一个空白框强。
        """
        c = store.get_collection(cid)
        if not c:
            self.send_error(404, "no collection")
            return
        data, _ct = self._cover_bytes(c)
        if not data:
            self.send_error(404, "cover unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")   # 换封面要立刻看见
        self.end_headers()
        self.wfile.write(data)

    def _cover_bytes(self, c):
        """拿这个合集的封面字节;没有就生成并落盘,顺带写回 cover 字段。"""
        cov = (c.get("cover") or "").strip()
        if cov:
            p = Path(cov).expanduser()
            if p.is_file():
                try:
                    return p.read_bytes(), (mimetypes.guess_type(p.name)[0]
                                            or "image/jpeg")
                except Exception as e:
                    slog.warning("读封面失败 %s: %s", p, e)
        try:
            data = coverart.generate(c.get("title"), c.get("author"))
        except Exception as e:
            slog.warning("生成封面失败 %s: %s", c.get("title"), e)
            return None, ""
        try:
            d = store.lib_dir() / "covers"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{c['id']}.jpg"
            p.write_bytes(data)
            store.update_collection(c["id"], cover=str(p))
        except Exception as e:
            slog.warning("封面落盘失败: %s", e)
        return data, "image/jpeg"

    def _cover_from_url(self, b):
        """从一个**链接**里把图抓出来给用户挑。

        用户的要求:封面不是"上来就搜",而是「我给一个链接,你去这个链接里
        把图获取下来」。所以这里是:给图片地址就直接用;给网页地址就把它
        里面的图都列出来(og:image / twitter:image 优先 —— 那是站点自己
        认定的代表图,比正文里随便一张配图靠谱得多)。
        """
        import requests
        from urllib.parse import urljoin
        raw = (b.get("url") or "").strip()
        if not raw.lower().startswith(("http://", "https://")):
            return {"ok": False, "err": "请填一个 http/https 开头的链接", "images": []}
        # ① 直接就是一张图
        if re.search(r"\.(jpe?g|png|webp|gif|bmp|avif)(\?|$)", raw, re.I):
            return {"ok": True, "images": [{"url": raw, "thumb": raw,
                                            "source": "直链", "title": ""}],
                    "kind": "image"}
        # ② 是一个网页 —— 把里面的图挑出来
        try:
            r = requests.get(raw, headers=COVER_UA, timeout=20)
            r.raise_for_status()
            page = r.text
        except Exception as e:
            return {"ok": False, "err": f"打不开这个链接: {str(e)[:80]}",
                    "images": []}
        return {"ok": True, "kind": "page",
                "images": self._images_in_page(page, raw, urljoin),
                "page_title": (re.search(r"<title[^>]*>(.*?)</title>",
                                         page, re.S | re.I) or [None, ""])[1].strip()[:120]}

    @staticmethod
    def _images_in_page(page, base, urljoin):
        """按可信度排序:og:image > twitter:image > 正文里够大的 <img>。"""
        out, seen = [], set()

        def add(u, src, w=0):
            if not u:
                return
            u = urljoin(base, u.strip())
            if not u.lower().startswith(("http://", "https://")):
                return
            if u in seen or re.search(r"\.svg(\?|$)", u, re.I):
                return
            seen.add(u)
            out.append({"url": u, "thumb": u, "source": src, "w": w})

        # ⚠️ 先整段切出 <meta ...> 再逐个解析**属性**,不要写「属性顺序固定」的正则。
        #    我第一版是 `<meta[^>]+property=…[^>]*content=…`,结果一条都匹配不上
        #    —— 页面里属性顺序、换行、大小写都不一定。于是 og:image 全落空,
        #    「站点自己认定的代表图」这条最有价值的线索被白白丢掉,
        #    24 张结果全落到"页面图片"里按宽度瞎排。
        for m in re.finditer(r"<meta\b[^>]*>", page, re.I | re.S):
            tag = m.group(0)
            k = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            v = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
            if not (k and v):
                continue
            key = k.group(1).strip().lower()
            if key in ("og:image", "og:image:secure_url", "og:image:url"):
                add(v.group(1), "og:image")
            elif key in ("twitter:image", "twitter:image:src"):
                add(v.group(1), "twitter:image")

        for m in re.finditer(r"<img\b[^>]*>", page, re.I | re.S):
            tag = m.group(0)
            w = 0
            wm = re.search(r'\bwidth\s*=\s*["\']?(\d+)', tag, re.I)
            if wm:
                w = int(wm.group(1))
            # srcset 里常放着真正的高清图 —— 只看 src 会拿到一张占位小图。
            # 取最后一个候选(通常最大)。
            ss = re.search(r'\bsrcset\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if ss:
                cands = [c.strip().split()[0] for c in ss.group(1).split(",")
                         if c.strip()]
                if cands:
                    add(cands[-1], "页面图片", w or 1200)
            src = (re.search(r'\bsrc\s*=\s*["\']([^"\']+)', tag, re.I) or
                   re.search(r'\bdata-src\s*=\s*["\']([^"\']+)', tag, re.I) or
                   re.search(r'\bdata-original\s*=\s*["\']([^"\']+)', tag, re.I))
            if src:
                add(src.group(1), "页面图片", w)
        # 大图排前面(og:image 永远最前)
        head = [x for x in out if x["source"] in ("og:image", "twitter:image")]
        tail = sorted([x for x in out if x not in head],
                      key=lambda x: -x.get("w", 0))
        return (head + tail)[:24]

    def _cover_search(self, b):
        q = (b.get("q") or "").strip()
        if not q:
            return {"ok": False, "err": "先输入关键词", "results": []}
        uniq, errs = search_covers(q)
        if not uniq and errs:
            # 全挂 ≠ 没搜到。分开告诉用户,不然会以为是关键词的问题
            return {"ok": False, "err": "; ".join(errs)[:300],
                    "results": [], "errs": errs}
        return {"ok": True, "results": uniq[:24], "errs": errs}

    def _store_cover(self, cid, raw):
        """把一张原始图**压上书名和作者**之后落盘。

        ⚠️ 两种做封面的方式(本地上传 / 从链接取)都走这里 ——
        用户的要求是「不论哪种方式,都要把书名和作者写在图片上面」。
        分开写两份的话,迟早只改一处。
        """
        c = store.get_collection(cid)
        if not c:
            return {"ok": False, "err": "合集不存在"}
        try:
            data = coverart.compose(raw, c.get("title"), c.get("author"))
        except Exception as e:
            # 合成失败也别把图丢了 —— 直接用原图,总比没有封面强
            slog.warning("封面合成失败 %s: %s", c.get("title"), e)
            data = raw
        d = store.lib_dir() / "covers"
        d.mkdir(parents=True, exist_ok=True)
        # ⚠️ 统一存 .jpg。以前按 Content-Type 存不同扩展名,
        #    换封面时旧文件留在那儿,变成「换过了但看起来没换」。
        for old in d.glob(f"{cid}.*"):
            if old.name != f"{cid}.jpg":
                try:
                    old.unlink()
                except Exception:
                    pass
        p = d / f"{cid}.jpg"
        try:
            p.write_bytes(data)
        except Exception as e:
            return {"ok": False, "err": f"保存失败: {str(e)[:80]}"}
        store.update_collection(cid, cover=str(p))
        return {"ok": True, "cover": str(p), "bytes": len(data)}

    def _download_cover(self, cid, url):
        """把一张在线图下载下来,压上书名/作者,设为封面。"""
        import requests
        if not cid or not (url or "").lower().startswith(("http://", "https://")):
            return {"ok": False, "err": "封面地址无效"}
        # ⚠️ 很多图床校验 Referer(防盗链),只发一个 UA 会吃 401/403。
        #    实测:从 Guardian 页面抓到的图,不带 Referer 直接 401
        #    「missing signature」。和抓正文一样退着试三个梯队。
        from urllib.parse import urlparse as _up
        u = _up(url)
        origin = f"{u.scheme}://{u.netloc}/"
        raw, why = b"", "下载失败"
        for hdr in ({"User-Agent": COVER_UA["User-Agent"], "Referer": origin},
                    dict(COVER_UA),
                    {"User-Agent": "Mozilla/5.0 (compatible; CEVTUO-RWP/1.3)",
                     "Referer": origin}):
            try:
                r = requests.get(url, headers=hdr, timeout=20,
                                 allow_redirects=True)
                if r.status_code == 200 and r.content:
                    raw = r.content
                    break
                why = f"HTTP {r.status_code}"
            except Exception as e:
                why = str(e)[:80]
        if not raw:
            return {"ok": False, "err": why}
        if len(raw) < 800:
            return {"ok": False, "err": "图片太小,可能不是封面"}
        return self._store_cover(cid, raw)

    def _save_cover_url(self, b):
        return self._download_cover(b.get("id"), b.get("url"))

    def _fill_missing_covers(self, b):
        """给还没有封面的合集批量补一张。

        ⚠️ 重点不是"能补",是**把没补上的也报出来**。
        用户原话:「下载图片没有反馈,是否哪些没下载下来,要不要补充等
        (专门把没获取到的再获取一遍)」。所以这里回两份名单:
        filled 补成功的、failed 没补上的(带原因),前端照单重试。
        """
        only = b.get("ids") or None
        got, failed = [], []
        for c in store.list_collections():
            if only is not None and c["id"] not in only:
                continue
            if (c.get("cover") or "").strip():
                continue
            q = (c.get("title") or "").strip()
            if not q:
                failed.append({"id": c["id"], "title": c["title"],
                               "why": "合集没有名字,不知道搜什么"})
                continue
            try:
                cands, errs = search_covers(q, 6)
            except Exception as e:
                failed.append({"id": c["id"], "title": c["title"],
                               "why": f"搜索出错: {str(e)[:60]}"})
                continue
            if not cands:
                failed.append({"id": c["id"], "title": c["title"],
                               "why": "四个源都没搜到图" if not errs
                               else f"搜索源出错({errs[0][:50]})"})
                continue
            # 前几张都试一下 —— 搜到的第一张经常下不下来(防盗链/404)
            why = "没有一张能下载"
            for cand in cands[:4]:
                res = self._download_cover(c["id"], cand["url"])
                if res.get("ok"):
                    got.append({"id": c["id"], "title": c["title"],
                                "source": cand["source"],
                                "bytes": res.get("bytes")})
                    why = ""
                    break
                why = res.get("err") or why
            if why:
                failed.append({"id": c["id"], "title": c["title"], "why": why})
        return {"ok": True, "filled": got, "failed": failed,
                "total": len(got) + len(failed)}

    def _save_cover(self, b):
        cid, data = b.get("id"), b.get("data_url") or ""
        if not cid or "," not in data:
            return {"ok": False, "err": "没有图片数据"}
        _head, b64 = data.split(",", 1)
        try:
            raw = base64.b64decode(b64)
        except Exception as e:
            return {"ok": False, "err": f"图片数据坏了: {str(e)[:60]}"}
        if len(raw) < 400:
            return {"ok": False, "err": "这张图太小了"}
        return self._store_cover(cid, raw)


def main():
    ap = argparse.ArgumentParser(description="CEVTUO-RWP 管理界面")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        log("端口被占用:已有一个实例在运行,直接打开页面")
        if args.open:
            subprocess.Popen(["open", url])
        return
    log(f"CEVTUO-RWP 管理界面: {url}")
    if args.open:
        threading.Timer(0.8, partial(subprocess.Popen, ["open", url])).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
