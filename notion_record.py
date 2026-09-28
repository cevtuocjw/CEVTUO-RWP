#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 CEVTUO-RWP2EPUB 的项目记录写进 Notion 的「epub app 项目」页面。

图片上传直接复用你 ~/Library/Scripts/notion-clip.py 里已经跑通的那套
(file_uploads 两步流程),不重复实现。
"""
import importlib.util
import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    CTX = ssl.create_default_context()

SHOTS = Path("/tmp/cevtuo_shots")
PAGE_ID = "3e209462-a4af-80d9-a313-c5862a780559"
API = "https://api.notion.com/v1"


def load_token():
    for p in (os.path.expanduser("~/.coof-movie/config.json"),
              os.path.expanduser("~/.notion-clip/config.json")):
        try:
            t = json.load(open(p)).get("notion_token")
            if t:
                return t
        except Exception:
            pass
    raise SystemExit("找不到 notion_token")


TOKEN = load_token()


def api(method, path, body=None, raw=None, ctype="application/json",
        version="2022-06-28"):
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + TOKEN,
                                          "Notion-Version": version,
                                          "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=120, context=CTX) as r:
        return json.loads(r.read().decode())


def upload(path, max_w=1700):
    """压缩后上传,返回 file_upload id。"""
    from PIL import Image
    import io
    p = Path(path)
    im = Image.open(p)
    if im.width > max_w:
        h = int(im.height * max_w / im.width)
        im = im.resize((max_w, h), Image.LANCZOS)
    buf = io.BytesIO()
    if p.suffix.lower() == ".gif":
        data = p.read_bytes()
    else:
        im.convert("RGB").save(buf, "JPEG", quality=86, optimize=True)
        data = buf.getvalue()
    fname = p.stem + (".gif" if p.suffix.lower() == ".gif" else ".jpg")
    ctype = "image/gif" if p.suffix.lower() == ".gif" else "image/jpeg"
    up = api("POST", "/file_uploads",
             {"filename": fname, "content_type": ctype},
             version="2025-09-03")
    uid = up["id"]
    b = "----cevtuo"
    body = b"".join([
        (f"--{b}\r\n").encode(),
        (f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n').encode(),
        (f"Content-Type: {ctype}\r\n\r\n").encode(),
        data, b"\r\n", (f"--{b}--\r\n").encode()])
    r = api("POST", f"/file_uploads/{uid}/send", raw=body,
            ctype=f"multipart/form-data; boundary={b}", version="2025-09-03")
    ok = r.get("status") == "uploaded"
    print("    %s %-24s %6.0f KB" % ("✓" if ok else "✗", fname, len(data) / 1024))
    return uid if ok else None


def rt(s, bold=False, code=False, link=None, color=None):
    o = {"type": "text", "text": {"content": s}}
    if link:
        o["text"]["link"] = {"url": link}
    ann = {}
    if bold:
        ann["bold"] = True
    if code:
        ann["code"] = True
    if color:
        ann["color"] = color
    if ann:
        o["annotations"] = ann
    return o


def para(*parts):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": list(parts)}}


def h2(s):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [rt(s)]}}


def h3(s):
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [rt(s)]}}


def bullet(*parts):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": list(parts)}}


def numbered(*parts):
    return {"object": "block", "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": list(parts)}}


def divider():
    return {"object": "block", "type": "divider", "divider": {}}


def img(uid, caption=None):
    b = {"object": "block", "type": "image",
         "image": {"type": "file_upload", "file_upload": {"id": uid}}}
    if caption:
        b["image"]["caption"] = [rt(caption)]
    return b


def callout(text, emoji="💡"):
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": [rt(text)], "icon": {"emoji": emoji}}}


def append(blocks):
    for i in range(0, len(blocks), 50):
        api("PATCH", f"/blocks/{PAGE_ID}/children",
            {"children": blocks[i:i + 50]})
        time.sleep(0.4)


def main():
    print("=== 上传图片 ===")
    shots = [
        ("01-collections", "合集列表 —— 一个合集 = 一本电子书"),
        ("02-collection-detail", "章节管理 —— 每篇文章是一个章节,可排序/删除/逐页编辑"),
        ("03-import-dialog", "导入面板 —— 多个网址 / 网站内链接 / RSS / Markdown 四条路径"),
        ("04-article-view", "逐页查看与编辑 —— 预览 / 所见即所得 / HTML 源码三种模式"),
        ("05-rss", "Rssdailyepub —— 一键生成、定时设置、100 个源的分类管理"),
        ("06-library", "rssdailyepub书库 —— 每日书按日期成文件夹"),
        ("07-settings", "设置 —— 主题/语言/玻璃质感、书库位置、浏览器渲染状态"),
        ("20-pages", "项目主页(GitHub Pages)"),
        ("21-repo", "GitHub 仓库"),
    ]
    ids = {}
    for name, cap in shots:
        p = SHOTS / f"{name}.png"
        if p.exists():
            ids[name] = upload(p)
    gif = upload(SHOTS / "walkthrough.gif")
    print("  GIF:", "✓" if gif else "✗")

    print("\n=== 写入 Notion 页面 ===")
    B = []
    B.append(h2("CEVTUO-RWP2EPUB · 项目记录"))
    B.append(para(rt("把网页、网站内链接、RSS 订阅、Markdown 做成 EPUB 电子书。"
                     "原生桌面应用,自带运行时,下载即用。")))
    B.append(para(rt("author: ", bold=True), rt("cevtuo")))
    B.append(divider())

    B.append(h3("链接"))
    B.append(bullet(rt("项目主页  ", bold=True),
                    rt("https://apps.cevtuogrnd.com/CEVTUO-RWP2EPUB/",
                       link="https://apps.cevtuogrnd.com/CEVTUO-RWP2EPUB/")))
    B.append(bullet(rt("GitHub  ", bold=True),
                    rt("https://github.com/cevtuocjw/CEVTUO-RWP2EPUB",
                       link="https://github.com/cevtuocjw/CEVTUO-RWP2EPUB")))
    B.append(bullet(rt("下载  ", bold=True),
                    rt("macOS .dmg(约 100 MB,Apple Silicon) / "
                       "Windows .zip(约 37 MB,Win10/11 x64)")))
    B.append(divider())

    B.append(h3("功能点"))
    B.append(para(rt("CEVTUO合集坊", bold=True), rt(" —— 一个合集 = 一本电子书,每篇文章 = 一个章节")))
    for x in ["多个网址:一行一个批量抓取",
              "网站内链接:列出页面内链接或 sitemap.xml,勾选导入",
              "RSS 订阅:列出条目勾选导入",
              "Markdown:用 # 一级标题分篇",
              "章节管理:排序、删除、空白页、随时从网址补一页",
              "逐页查看与编辑:预览 / 所见即所得 / HTML 源码",
              "导出 EPUB:封面、作者、语言、前言目录、图片内嵌",
              "没有页数限制"]:
        B.append(bullet(rt(x)))

    B.append(para(rt("Rssdailyepub", bold=True), rt(" —— 原有的 RSS 自动化能力")))
    for x in ["一键生成每日书:选几天内、每源几条、哪些分类,全程进度可见",
              "定时:每天 / 每隔两天 / 每隔三天 / 取消,时间可改",
              "RSS 源管理:分类与源的增删改、导入 OPML(当前 8 类 100 源)"]:
        B.append(bullet(rt(x)))

    B.append(para(rt("rssdailyepub书库", bold=True)))
    for x in ["合集电子书按合集分文件夹",
              "每日书按日期分文件夹,点进去看当天的每一本",
              "书库位置可改,改动时已生成的书自动搬过去"]:
        B.append(bullet(rt(x)))
    B.append(divider())

    B.append(h3("技术要点"))
    B.append(para(rt("全文获取是整个项目的核心难点。做法是复制一份用户自己浏览器的 "
                     "profile(连同已装扩展与登录态),用它启动带调试端口的浏览器,"
                     "通过 CDP 打开页面、等 JS 渲染完、读回 DOM。")))
    B.append(bullet(rt("Chrome 136 起禁止对默认用户目录开 "),
                    rt("--remote-debugging-port", code=True),
                    rt(",必须换 user-data-dir —— 老式的"
                       "「带调试端口重启 Chrome」做法已失效")))
    B.append(bullet(rt("开发者模式加载的未打包扩展(如 Bypass Paywalls Clean)"
                       "光复制 profile 会「加载了但 host 权限为空」,"
                       "必须额外用 "),
                    rt("--load-extension=", code=True), rt("显式加载才会授权")))
    B.append(bullet(rt("正文抽取三级兜底:trafilatura → 最大文本块 → 整页 body")))
    B.append(bullet(rt("安装包自带 Python 运行时与全部依赖,目标机器零依赖")))
    B.append(bullet(rt("程序目录与数据目录分离:源码可待在只读的 .app 里,"
                       "数据写到 "), rt("~/Library/Application Support/CEVTUO-RWP2EPUB", code=True)))
    B.append(divider())

    B.append(h3("踩过的坑(记录备查)"))
    B.append(numbered(rt("抠图:用洪水填充去背景要按**邻居**比较而不是按种子点,"
                         "否则跨不过渐变背景,会留一大片不透明残留")))
    B.append(numbered(rt("zip 里的中文文件名必须打 UTF-8 标记,否则 Windows "
                         "资源管理器显示乱码(macOS 的 zip 命令不会自动加)")))
    B.append(numbered(rt(".cmd 脚本内容要用纯 ASCII —— cmd.exe 在中文 Windows 下"
                         "按 GBK 解析,UTF-8 中文会被误解析")))
    B.append(numbered(rt("python.org 装的 Python 不自带 CA 证书链,调 HTTPS API "
                         "会报 CERTIFICATE_VERIFY_FAILED,要显式用 certifi")))
    B.append(numbered(rt("DNS 没有「路径」概念;GitHub Pages 的项目站只有在"
                         "用户站设了自定义域名时才会挂在 /<仓库名>/ 下")))
    B.append(divider())

    if gif:
        B.append(h3("演示"))
        B.append(para(rt("(下面是一段界面走查动画,依次是:合集列表 → 章节管理 → "
                         "导入面板 → 逐页编辑 → RSS → 书库 → 设置 → 项目主页)")))
        B.append(img(gif))
        B.append(divider())

    B.append(h3("界面截图"))
    for name, cap in shots:
        if ids.get(name):
            B.append(img(ids[name], cap))
            B.append(para(rt(cap)))

    append(B)
    print("\n✓ 已写入页面: https://www.notion.so/" + PAGE_ID.replace("-", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
