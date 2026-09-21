# -*- coding: utf-8 -*-
"""HTML 清洗:去掉脚本/样式/事件属性,只留可安全进 EPUB 的正文标签。

rss2epub.py 与 extract_page.py 共用这一份实现。
"""
import re

_ALLOWED_KEEP = {
    "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code", "col",
    "colgroup", "dd", "del", "div", "dl", "dt", "em", "figcaption", "figure",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd", "li",
    "mark", "ol", "p", "pre", "q", "s", "samp", "small", "span", "strong",
    "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u",
    "ul", "video", "audio", "source",
}


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def clean_html(raw):
    if not raw:
        return ""
    try:
        from lxml import etree, html as lh
        try:
            from lxml_html_clean import clean_html as _lclean
        except Exception:
            try:
                from lxml.html.clean import clean_html as _lclean
            except Exception:
                _lclean = None
        try:
            doc = lh.document_fromstring(raw)
        except Exception:
            return "<p>%s</p>" % _esc(re.sub(r"<[^>]+>", "", raw))
        if _lclean:
            try:
                _lclean(doc)
            except Exception:
                pass
        for tag in doc.xpath("//script|//style|//iframe|//object|//embed"
                             "|//form|//head|//noscript|//svg|//link|//meta"
                             "|//base|//template"):
            tag.drop_tree()
        body = doc.body
        if body is None:
            return ""
        # 注意:必须从 body 的**子孙**开始,不能遍历到 body/html 自己
        # —— 早先把根 <html> 改名成 div 会导致 doc.body 变 None,整篇清空。
        for el in body.iterdescendants():
            if not isinstance(el.tag, str):
                continue
            for a in list(el.attrib):
                if a.startswith("on") or a in ("style", "class", "id", "srcset",
                                               "sizes", "loading", "decoding"):
                    del el.attrib[a]
                elif a in ("href", "src") and \
                        el.attrib[a].lstrip().lower().startswith("javascript:"):
                    del el.attrib[a]
            if el.tag not in _ALLOWED_KEEP:
                # 不认识的标签:去掉标签本身但**保留其中的文字与子节点**
                # (改名成 div 会破坏 table 等结构)
                try:
                    el.drop_tag()
                except Exception:
                    pass
        return "".join(etree.tostring(c, encoding="unicode", method="html")
                       for c in body)
    except Exception:
        txt = re.sub(r"<[^>]+>", "", raw)
        return "<p>%s</p>" % _esc(txt)


def plain_len(html):
    return len(re.sub(r"<[^>]+>", "", html or "").strip())


def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html or "").strip()
