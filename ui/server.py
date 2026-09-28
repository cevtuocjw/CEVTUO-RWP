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
APP_VERSION = "1.1"

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


def _delete_books_of(cid):
    """删合集时把它生成过的 epub 一起删掉。

    ⚠️ 只删**索引里明确记着属于它**的,以及老结构 books/<id>/ 里的。
    不做「按标题前缀删」—— 合集标题互为前缀时那会误删隔壁合集的书。
    """
    bdir = store.books_dir()
    idx = _read_index()
    gone = []
    for name, rec in list(idx.items()):
        if not isinstance(rec, dict) or rec.get("cid") != cid:
            continue
        p = bdir / name
        try:
            if p.exists():
                p.unlink()
                gone.append(str(p))
        except Exception as e:
            slog.warning("删书失败 %s: %s", p, e)
        idx.pop(name, None)
    _write_index(idx)
    d = bdir / cid                       # 老结构
    if d.is_dir():
        gone += [str(p) for p in d.glob("*.epub")]
        shutil.rmtree(d, ignore_errors=True)
    return gone


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

    export_epub.build(coll, arts, out, opts, on_progress=prog)
    _register_book(out, cid)             # 平铺之后靠索引认归属
    return {"path": str(out), "name": out.name, "chapters": len(arts),
            "size": out.stat().st_size}


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
            store.set_settings({
                "schedule_enabled": bool(b.get("enabled")),
                "schedule_mode": b.get("mode") or "daily",
                "schedule_hour": int(b.get("hour") or 9),
                "schedule_minute": int(b.get("minute") or 0),
            })
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
        """按合集 id 把封面图发出去。

        ⚠️ 必须走 HTTP,不能用 file://。界面是从 http://127.0.0.1:8611 加载的,
        浏览器会把 http 页面里的 file:// 子资源**直接拦掉**。
        表现就是「封面上传成功了、但显示不出来」—— 这个只有真的打开界面才看得见,
        接口层面全是 200。
        """
        c = store.get_collection(cid)
        cov = (c or {}).get("cover") or ""
        if not cov:
            self.send_error(404, "no cover")
            return
        p = Path(cov).expanduser()
        if not p.is_file():
            self.send_error(404, "cover missing")
            return
        try:
            data = p.read_bytes()
        except Exception as e:
            slog.warning("读封面失败 %s: %s", p, e)
            self.send_error(404, "cover unreadable")
            return
        ct = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")   # 换封面要立刻看见
        self.end_headers()
        self.wfile.write(data)

    def _cover_search(self, b):
        """三个源都试一遍,能出几个算几个。"""
        q = (b.get("q") or "").strip()
        if not q:
            return {"ok": False, "err": "先输入关键词", "results": []}
        results, errs = [], []
        for fn in (_cover_from_google, _cover_from_openlibrary,
                   _cover_from_commons):
            try:
                results += fn(q)
            except Exception as e:
                errs.append(f"{fn.__name__.replace('_cover_from_', '')}: {e}")
        seen, uniq = set(), []
        for it in results:
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            uniq.append(it)
        if not uniq and errs:
            # 三个源全挂 ≠ 没有结果。要分开告诉用户,不然会以为是关键词的问题
            return {"ok": False, "err": "; ".join(errs)[:300], "results": []}
        return {"ok": True, "results": uniq[:24], "errs": errs}

    def _save_cover_url(self, b):
        """把在线搜到的封面**下载下来存本地**。

        ⚠️ 不把远程 URL 直接写进 cover 字段:界面是按 file:// 加载本地图的,
        存 URL 的话对方一改防盗链、或者用户离线,封面就变成一片空白。
        """
        import requests
        cid, url = b.get("id"), (b.get("url") or "").strip()
        if not cid or not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "err": "封面地址无效"}
        try:
            r = requests.get(url, headers=COVER_UA, timeout=20)
            r.raise_for_status()
            data = r.content
        except Exception as e:
            return {"ok": False, "err": f"下载封面失败: {e}"}
        if len(data) < 800:
            return {"ok": False, "err": "这张图太小,可能不是封面"}
        ct = (r.headers.get("Content-Type") or "").lower()
        ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
        d = store.lib_dir() / "covers"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{cid}{ext}"
        try:
            p.write_bytes(data)
        except Exception as e:
            return {"ok": False, "err": f"保存封面失败: {e}"}
        store.update_collection(cid, cover=str(p))
        return {"ok": True, "cover": str(p), "bytes": len(data)}

    def _save_cover(self, b):
        cid, data = b.get("id"), b.get("data_url") or ""
        if not cid or "," not in data:
            return {"ok": False, "err": "没有图片数据"}
        head, b64 = data.split(",", 1)
        ext = ".png"
        m = re.search(r"image/(\w+)", head)
        if m:
            ext = "." + ("jpg" if m.group(1) == "jpeg" else m.group(1))
        d = store.lib_dir() / "covers"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{cid}{ext}"
        p.write_bytes(base64.b64decode(b64))
        store.update_collection(cid, cover=str(p))
        return {"ok": True, "cover": str(p)}


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
