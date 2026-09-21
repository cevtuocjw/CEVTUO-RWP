# -*- coding: utf-8 -*-
"""CEVTUO-RWP2EPUB 数据层:合集 / 章节(文章) / RSS 源 / 设置。

对标 EpubKit 的 collections + articles 两表,但**没有 10 页导出上限**。
数据库: state/cevtuo.db
"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import paths

BASE = paths.ensure()          # 数据目录(可写);源码在 paths.CODE_DIR
DB_PATH = BASE / "state" / "cevtuo.db"

_lk = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
  id TEXT PRIMARY KEY,
  title TEXT,
  author TEXT,
  cover TEXT,
  language_code TEXT DEFAULT 'zh-CN',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY,
  collection_id TEXT,
  title TEXT,
  html TEXT,
  parsed_html TEXT,
  url TEXT,
  order_in_collection INTEGER DEFAULT 0,
  source TEXT,
  mode TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_collection ON articles(collection_id, order_in_collection);
CREATE TABLE IF NOT EXISTS rss_feeds (
  id TEXT PRIMARY KEY,
  title TEXT,
  url TEXT,
  category TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_id():
    return uuid.uuid4().hex[:12]


def conn():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def q(sql, args=()):
    with _lk:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur


def rows(sql, args=()):
    with _lk:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


# ------------------------------------------------------------- collections ---
def list_collections():
    out = rows("SELECT * FROM collections ORDER BY created_at DESC")
    for c in out:
        r = one("SELECT COUNT(*) n,"
                " SUM(CASE WHEN parsed_html IS NOT NULL AND parsed_html<>'' THEN 1 ELSE 0 END) ready"
                " FROM articles WHERE collection_id=?", (c["id"],))
        c["count"] = r["n"] or 0
        c["ready"] = r["ready"] or 0
    return out


def get_collection(cid):
    return one("SELECT * FROM collections WHERE id=?", (cid,))


def create_collection(title, author=None, language_code="zh-CN", cover=None):
    cid = new_id()
    q("INSERT INTO collections(id,title,author,cover,language_code,created_at)"
      " VALUES(?,?,?,?,?,?)", (cid, title or "未命名合集", author, cover,
                               language_code or "zh-CN", now()))
    return get_collection(cid)


def update_collection(cid, **kw):
    fields = {k: v for k, v in kw.items()
              if k in ("title", "author", "language_code", "cover")}
    if not fields:
        return get_collection(cid)
    sets = ",".join(f"{k}=?" for k in fields)
    q(f"UPDATE collections SET {sets} WHERE id=?", (*fields.values(), cid))
    return get_collection(cid)


def delete_collection(cid):
    q("DELETE FROM articles WHERE collection_id=?", (cid,))
    q("DELETE FROM collections WHERE id=?", (cid,))


# ---------------------------------------------------------------- articles ---
def list_articles(cid, with_body=False):
    cols = "*" if with_body else ("id,collection_id,title,url,order_in_collection,"
                                  "source,mode,created_at,"
                                  "LENGTH(COALESCE(parsed_html,'')) AS body_len")
    out = rows(f"SELECT {cols} FROM articles WHERE collection_id=?"
               " ORDER BY order_in_collection ASC, created_at ASC", (cid,))
    for i, a in enumerate(out):
        a["index"] = i + 1
    return out


def get_article(aid):
    return one("SELECT * FROM articles WHERE id=?", (aid,))


def next_order(cid):
    r = one("SELECT COALESCE(MAX(order_in_collection),-1)+1 n FROM articles"
            " WHERE collection_id=?", (cid,))
    return r["n"] if r else 0


def add_article(cid, title, parsed_html, url=None, html=None, source=None,
                mode=None):
    aid = new_id()
    q("INSERT INTO articles(id,collection_id,title,html,parsed_html,url,"
      "order_in_collection,source,mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
      (aid, cid, title or "未命名", html, parsed_html, url, next_order(cid),
       source, mode, now()))
    return get_article(aid)


def add_articles_bulk(cid, items, source=None):
    """items: [{title, parsed_html, url, html, mode}] —— 一次事务写多篇。"""
    if not items:
        return []
    start = next_order(cid)
    out = []
    with _lk:
        c = conn()
        for i, it in enumerate(items):
            aid = new_id()
            c.execute(
                "INSERT INTO articles(id,collection_id,title,html,parsed_html,"
                "url,order_in_collection,source,mode,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (aid, cid, it.get("title") or "未命名", it.get("html"),
                 it.get("parsed_html"), it.get("url"), start + i, source,
                 it.get("mode"), now()))
            out.append(aid)
        c.commit()
    return out


def update_article(aid, **kw):
    fields = {k: v for k, v in kw.items()
              if k in ("title", "parsed_html", "url", "html", "mode")}
    if not fields:
        return get_article(aid)
    sets = ",".join(f"{k}=?" for k in fields)
    q(f"UPDATE articles SET {sets} WHERE id=?", (*fields.values(), aid))
    return get_article(aid)


def delete_article(aid):
    a = get_article(aid)
    q("DELETE FROM articles WHERE id=?", (aid,))
    if a:
        reorder(a["collection_id"])


def move_article(aid, delta):
    """上移/下移一位(重排整个合集的 order,保证连续)。"""
    a = get_article(aid)
    if not a:
        return
    lst = rows("SELECT id FROM articles WHERE collection_id=?"
               " ORDER BY order_in_collection ASC, created_at ASC",
               (a["collection_id"],))
    ids = [x["id"] for x in lst]
    if aid not in ids:
        return
    i = ids.index(aid)
    j = max(0, min(len(ids) - 1, i + int(delta)))
    if i == j:
        return
    ids.insert(j, ids.pop(i))
    _apply_order(a["collection_id"], ids)


def reorder(cid, ids=None):
    if ids is None:
        ids = [x["id"] for x in rows(
            "SELECT id FROM articles WHERE collection_id=?"
            " ORDER BY order_in_collection ASC, created_at ASC", (cid,))]
    _apply_order(cid, ids)


def _apply_order(cid, ids):
    with _lk:
        c = conn()
        for i, aid in enumerate(ids):
            c.execute("UPDATE articles SET order_in_collection=?"
                      " WHERE id=? AND collection_id=?", (i, aid, cid))
        c.commit()


# --------------------------------------------------------------- settings ----
DEFAULT_SETTINGS = {
    "app_name": "CEVTUO-RWP2EPUB",
    # 书库根目录(空 = 程序所在目录)。RSS 日报写 <root>/output,
    # 合集导出写 <root>/books。用户可在界面上改,改的时候旧书会一起搬过去。
    "library_dir": "",
    "ui": {"theme": "auto", "glass": 0.62, "lang": "zh"},
    # 导入
    "use_chrome": True,            # 用真实 Chrome(带 BPC 扩展/登录态)渲染取正文
    "chrome_port": 9222,
    # 定时
    "schedule_enabled": True,
    "schedule_mode": "daily",      # daily | every2 | every3 | off
    "schedule_hour": 9,
    "schedule_minute": 0,
    "window_days": 1,              # 取最近 N 天内容
    "schedule_categories": [],     # 空 = 全部分类
    "max_per_feed": 15,
    "send_email": False,
}


def get_settings():
    out = dict(DEFAULT_SETTINGS)
    for r in rows("SELECT key,value FROM settings"):
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            out[r["key"]] = r["value"]
    return out


def lib_dir():
    """书库根目录。空/未设置时就是程序自己的目录(与旧结构兼容)。"""
    raw = (get_settings().get("library_dir") or "").strip()
    if not raw:
        return BASE
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (BASE / p)


def output_dir():
    return lib_dir() / "output"


def books_dir():
    return lib_dir() / "books"


def set_settings(patch):
    with _lk:
        c = conn()
        for k, v in (patch or {}).items():
            c.execute("INSERT INTO settings(key,value) VALUES(?,?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, json.dumps(v, ensure_ascii=False)))
        c.commit()
    return get_settings()


# ------------------------------------------------------------- rss feeds -----
def list_rss_feeds():
    return rows("SELECT * FROM rss_feeds ORDER BY category, title")


def add_rss_feed(title, url, category):
    fid = new_id()
    q("INSERT INTO rss_feeds(id,title,url,category,created_at) VALUES(?,?,?,?,?)",
      (fid, title or url, url, category or "未分类", now()))
    return one("SELECT * FROM rss_feeds WHERE id=?", (fid,))


def update_rss_feed(fid, **kw):
    fields = {k: v for k, v in kw.items() if k in ("title", "url", "category")}
    if fields:
        sets = ",".join(f"{k}=?" for k in fields)
        q(f"UPDATE rss_feeds SET {sets} WHERE id=?", (*fields.values(), fid))
    return one("SELECT * FROM rss_feeds WHERE id=?", (fid,))


def delete_rss_feed(fid):
    q("DELETE FROM rss_feeds WHERE id=?", (fid,))
