# -*- coding: utf-8 -*-
"""feeds.opml 的读写(RSS 源管理)。

结构:body/outline[有 xmlUrl 的是一级散源;有子 outline 的是分类]。
与 rss2epub.parse_opml 保持同一套约定。
"""
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_CATEGORY = "未分类"


def _title(el):
    return el.get("title") or el.get("text") or ""


def read(path):
    """返回 [{"name": 分类名, "feeds": [{"title","url"}]}],散源归入「未分类」。"""
    p = Path(path)
    if not p.exists():
        return []
    root = ET.parse(str(p)).getroot()
    body = root.find("body")
    if body is None:
        return []
    cats, loose = [], []
    for out in body.findall("outline"):
        if out.get("xmlUrl"):
            loose.append({"title": _title(out), "url": out.get("xmlUrl")})
            continue
        feeds = [{"title": _title(o), "url": o.get("xmlUrl")}
                 for o in out.iter("outline") if o.get("xmlUrl")]
        if feeds:
            cats.append({"name": _title(out) or DEFAULT_CATEGORY,
                         "feeds": feeds})
    if loose:
        for c in cats:
            if c["name"] == DEFAULT_CATEGORY:
                c["feeds"].extend(loose)
                loose = []
        if loose:
            cats.append({"name": DEFAULT_CATEGORY, "feeds": loose})
    return cats


def write(path, cats):
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "CEVTUO-RWP2EPUB 订阅源"
    body = ET.SubElement(root, "body")
    for c in cats or []:
        feeds = c.get("feeds") or []
        if not feeds:
            continue
        if c.get("name") == DEFAULT_CATEGORY:
            # 未分类:写成一級散源,和 parse_opml 的兜底一致
            for f in feeds:
                ET.SubElement(body, "outline", {
                    "text": f.get("title") or f.get("url"),
                    "title": f.get("title") or f.get("url"),
                    "type": "rss", "xmlUrl": f.get("url")})
            continue
        o = ET.SubElement(body, "outline", {
            "text": c["name"], "title": c["name"]})
        for f in feeds:
            ET.SubElement(o, "outline", {
                "text": f.get("title") or f.get("url"),
                "title": f.get("title") or f.get("url"),
                "type": "rss", "xmlUrl": f.get("url")})
    _indent(root)
    Path(path).write_bytes(ET.tostring(root, encoding="utf-8",
                                       xml_declaration=True))


def _indent(el, level=0):
    pad = "\n" + "  " * level
    if len(el):
        if not (el.text or "").strip():
            el.text = pad + "  "
        for c in el:
            _indent(c, level + 1)
        if not (el[-1].tail or "").strip():
            el[-1].tail = pad
    if level and not (el.tail or "").strip():
        el.tail = pad


# ------------------------------------------------------------ 便捷操作 -------
def add_feed(path, category, title, url):
    cats = read(path)
    for c in cats:
        if c["name"] == category:
            c["feeds"].append({"title": title or url, "url": url})
            break
    else:
        cats.append({"name": category, "feeds": [
            {"title": title or url, "url": url}]})
    write(path, cats)
    return cats


def update_feed(path, url, new_url=None, new_title=None, new_category=None,
                old_category=None):
    cats = read(path)
    for c in cats:
        for f in c["feeds"]:
            if f["url"] == url:
                f["url"] = new_url or f["url"]
                f["title"] = new_title or f["title"]
                if new_category and new_category != c["name"]:
                    c["feeds"].remove(f)
                    return add_feed(path, new_category, f["title"], f["url"])
                write(path, cats)
                return cats
    return cats


def delete_feed(path, url):
    cats = read(path)
    for c in cats:
        c["feeds"] = [f for f in c["feeds"] if f["url"] != url]
    write(path, [c for c in cats if c["feeds"]])
    return read(path)


def add_category(path, name):
    cats = read(path)
    if not any(c["name"] == name for c in cats):
        cats.append({"name": name, "feeds": []})
    write(path, cats)
    return cats


def rename_category(path, old, new):
    cats = read(path)
    for c in cats:
        if c["name"] == old:
            c["name"] = new
    write(path, cats)
    return read(path)


def delete_category(path, name):
    """删分类但保留源(移到未分类)。"""
    cats = read(path)
    moved = []
    for c in cats:
        if c["name"] == name:
            moved.extend(c["feeds"])
    cats = [c for c in cats if c["name"] != name]
    for c in cats:
        if c["name"] == DEFAULT_CATEGORY:
            c["feeds"].extend(moved)
            moved = []
    if moved:
        cats.append({"name": DEFAULT_CATEGORY, "feeds": moved})
    write(path, cats)
    return read(path)
