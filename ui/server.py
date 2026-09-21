#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEVTUO-RWP2EPUB —— 统一 Web 界面服务端。

A. CEVTUO合集坊(对标 EpubKit):多个网址 / 网站内链接 / RSS / Markdown 导入,
   章节管理、逐页查看与编辑、导出 EPUB(无 10 页限制)
B. Rssdailyepub: RSS 源管理、一键生成、进度、定时

启动: ./.venv/bin/python ui/server.py [--port 8611] [--open]
"""
import argparse
import base64
import glob
import json
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
def library():
    """书库:合集书 + Rssdailyepub 生成的书(按日期成文件夹)。"""
    bdir = store.books_dir()
    colls = []
    for c in store.list_collections():
        d = bdir / c["id"]
        books = [file_info(p) for p in sorted(d.glob("*.epub"),
                                              key=lambda p: p.stat().st_mtime,
                                              reverse=True)] if d.exists() else []
        c["books"] = books
        colls.append(c)

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
    return {"collections": colls, "rss": rss,
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
    d = store.books_dir() / cid
    d.mkdir(parents=True, exist_ok=True)
    name = f"{sanitize_filename(coll['title'])}-{time.strftime('%Y%m%d-%H%M')}.epub"
    out = d / name
    ctx.log(f"导出「{coll['title']}」(共 {len(arts)} 章)")
    ctx.set_total(len(arts))

    def prog(done, total, msg=None):
        if msg:
            ctx.progress(done, total, msg)
        else:
            ctx.progress(done, total)

    export_epub.build(coll, arts, out, opts, on_progress=prog)
    return {"path": str(out), "name": name, "chapters": len(arts),
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
                    "app": "CEVTUO-RWP2EPUB",
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
            store.delete_collection(b["id"])
            d = store.books_dir() / b["id"]
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            return {"ok": True}
        if r == "/api/collection/cover":
            return self._save_cover(b)

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
                                  mode=art["mode"])
            return {"ok": True, "article": a}

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
    ap = argparse.ArgumentParser(description="CEVTUO-RWP2EPUB 管理界面")
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
    log(f"CEVTUO-RWP2EPUB 管理界面: {url}")
    if args.open:
        threading.Timer(0.8, partial(subprocess.Popen, ["open", url])).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
