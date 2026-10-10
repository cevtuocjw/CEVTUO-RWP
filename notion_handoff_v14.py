#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 v1.4 这批修复追加到 Notion「epub app 项目」页面。

只追加,不删已有内容。⚠️ Notion 单次请求 ≤100 block,这里按 50 一批发。
"""
import json
import os
import ssl
import time
import urllib.request

try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    CTX = ssl.create_default_context()

PAGE_ID = "3e209462-a4af-80d9-a313-c5862a780559"
API = "https://api.notion.com/v1"


def load_token():
    for p in (os.path.expanduser("~/.notion-clip/config.json"),
              os.path.expanduser("~/.coof-movie/config.json")):
        try:
            t = json.load(open(p)).get("notion_token")
            if t:
                return t
        except Exception:
            pass
    raise SystemExit("找不到 notion_token")


TOKEN = load_token()


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Notion-Version": "2022-06-28",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return json.loads(r.read().decode())


def rt(s, bold=False, code=False, color=None):
    o = {"type": "text", "text": {"content": str(s)[:1900]}}
    a = {}
    if bold:
        a["bold"] = True
    if code:
        a["code"] = True
    if color:
        a["color"] = color
    if a:
        o["annotations"] = a
    return o


def para(*p):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": list(p) or [rt("")]}}


def h1(s):
    return {"object": "block", "type": "heading_1", "heading_1": {"rich_text": [rt(s)]}}


def h2(s):
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [rt(s)]}}


def h3(s):
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [rt(s)]}}


def bullet(*p):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": list(p)}}


def num(*p):
    return {"object": "block", "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": list(p)}}


def todo(s):
    return {"object": "block", "type": "to_do",
            "to_do": {"rich_text": [rt(s)], "checked": False}}


def code_block(s, lang="plain text"):
    return {"object": "block", "type": "code",
            "code": {"rich_text": [rt(s)], "language": lang}}


def divider():
    return {"object": "block", "type": "divider", "divider": {}}


def callout(s, emoji="⚠️", color="yellow_background"):
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": [rt(s)], "icon": {"emoji": emoji},
                        "color": color}}


def append(blocks):
    for i in range(0, len(blocks), 50):
        api("PATCH", f"/blocks/{PAGE_ID}/children",
            {"children": blocks[i:i + 50]})
        time.sleep(0.4)
        print(f"    已写入 {min(i + 50, len(blocks))}/{len(blocks)}")


B = []
B.append(divider())
B.append(h1("🔧 v1.4 修复记录 · 2026-10-10"))
B.append(para(rt("用户报的三件事,查下来都是真 bug,而且都不是网络问题。", bold=True)))

# ---------------------------------------------------------------- ①
B.append(h2("① 「给的链接做不出合集」"))
B.append(para(rt("根因:", bold=True),
              rt("用户粘的是 "), rt("[标题](https://…)", code=True),
              rt(" 这种 Markdown 链接格式,程序整串当网址丢给 requests →"),
              rt("No connection adapters were found for '[标题](https://…)'", code=True),
              rt(" → 界面显示「抓取失败」。看起来像网络问题,其实是格式没解析。")))
B.append(para(rt("修:", bold=True), rt(" "),
              rt("imports.normalize_urls()", code=True),
              rt(",支持:裸网址 / Markdown 链接 / 尖括号 / 「标题 网址」混排 / "
                 "一行多个 / 中文标点分隔。")))
B.append(callout("写这个函数时踩了两个坑:\n"
                 "· 找到 Markdown 链接就跳过裸网址扫描 ⇒ 一行里混着的裸网址被静默丢掉\n"
                 "· 字符类没排除中文标点 ⇒ "
                 "https://a.com/1，下一篇：https://b.com/2 被当成一个网址"))

# ---------------------------------------------------------------- ②
B.append(h2("② 「补图的时候又重做了一本书」"))
B.append(para(rt("根因:", bold=True),
              rt("导出时把下载失败的 <img> 直接删掉了 —— 于是「再补一次」根本无从下手"
                 "(不知道这里原本有图、原图是哪个网址)。而「重新获取」按钮走的是"
                 "重新导出,所以书库里多出一本,原来那本还是缺的。")))
B.append(para(rt("修(三处配合):", bold=True)))
B.append(num(rt("导出时失败的图**不删**,改留占位 "),
             rt('<span class="cevtuo-miss" data-cevtuo-miss="原URL"/>', code=True),
             rt("(样式 display:none,阅读器里不显示)")))
B.append(num(rt("新增 "), rt("export_epub.patch_missing_images(epub_path)", code=True),
             rt(" —— 打开原来那本 EPUB,只重下缺的、插进去、改 OPF manifest、"
                "写回同一个文件")))
B.append(num(rt("新接口 "), rt("POST /api/book/fillImages {path}", code=True),
             rt("(走 job,有进度),前端的「把没拿到的补进这本书」调它")))
B.append(callout("这个函数里踩了两个坑,都会让图「看起来补上了但其实是空的」:\n\n"
                 "· lxml 把空元素序列化成**自闭合** "
                 "<span …/> 而不是 <span></span> —— 正则只写对标签 ⇒ "
                 "占位明明在文件里,一条都匹配不上,永远报「缺 0 张」\n\n"
                 "· href 和 zippath **不是一回事**:manifest / <img src> 用相对 OPF 的 "
                 "images/img9.png,而 zip 里的条目是 EPUB/images/img9.png。混用 ⇒ "
                 "章节指向一张包里不存在的图,书能打开、图是空的,"
                 "而且 zipfile.testzip() 还是通过的(因为 zip 本身没坏)",
                 "⚠️", "red_background"))

# ---------------------------------------------------------------- ③
B.append(h2("③ 「反馈有点问题」"))
B.append(para(rt("根因:", bold=True),
              rt("报告数的是**去重后的网址数**,不是**书里的图片张数**。"
                 "同一张图出现在两篇文章里,读者会看到两处,报告却只算一张 —— "
                 "用户看到「书里明明 3 个 <img>,报告写需要 2 张」。")))
B.append(para(rt("修:", bold=True), rt(" "), rt("ImageEmbedder.report()", code=True),
              rt(" 现在回四个数,含义分开:")))
B.append(bullet(rt("wanted", code=True), rt(" —— 书里一共出现多少个 <img>(读者看到的张数)")))
B.append(bullet(rt("unique", code=True), rt(" —— 不同的地址几个(实际下载次数)")))
B.append(bullet(rt("ok", code=True), rt(" —— 下到了几个不同的图")))
B.append(bullet(rt("failed[].uses", code=True),
                rt(" —— 这个没下到的地址在书里出现几处")))
B.append(para(rt("界面文案相应改成:「书里一共 5 张图(来自 2 个地址)。"
                 "成功嵌入 1 张,1 个地址没获取到。」")))

# ---------------------------------------------------------------- 状态
B.append(h2("当前状态(2026-10-10)"))
B.append(bullet(rt("本机 app  ", bold=True), rt("v1.4,已装、已运行")))
B.append(bullet(rt("DMG / Windows 包  ", bold=True), rt("已重建在 build/dist/")))
B.append(bullet(rt("Release  ", bold=True, color="red"),
                rt("⚠️ 还是 v1.3 —— v1.4 没发布")))
B.append(bullet(rt("分发页  ", bold=True, color="red"),
                rt("⚠️ 还是 v1.3 那版(页面用 releases/latest/download/,发布后自动跟随)")))
B.append(bullet(rt("仓库  ", bold=True, color="red"),
                rt("⚠️ v1.4 的改动还没提交")))
B.append(h3("下次要做的事(按顺序)"))
B.append(num(rt("cd build && git add -A && git commit && git pull --rebase && git push")))
B.append(num(rt("gh release create v1.4 dist/CEVTUO-RWP-macOS.dmg "
                "dist/CEVTUO-RWP-windows.zip")))
B.append(num(rt("检查它不是 Draft —— Draft 不会成为 latest,下载链接会 404")))
B.append(h3("还没解决的"))
B.append(todo("https://apps.cevtuogrnd.com 打不开(缺 GitHub 的 TXT 校验记录,"
              "值只在网页 Settings → Pages 里显示)"))
B.append(todo("Windows 包从未在真实 Windows 上跑过"))

# ---------------------------------------------------------------- 可粘贴的话
B.append(h2("📌 贴进新窗口的第一句话"))
B.append(code_block(
    "读 ~/Library/Application Support/RSSDailyEpub/HANDOFF.md,然后继续处理。\n"
    "当前是 v1.4,已装在本机但还没发布(Release 上最新是 v1.3)。", "plain text"))
B.append(callout("交接文档母版在 "
                 "~/Library/Application Support/RSSDailyEpub/HANDOFF.md,"
                 "以后每次改完都同步更新它。", "📄", "blue_background"))

print(f"=== 共 {len(B)} 个 block ===")
append(B)
print("完成 → https://www.notion.so/" + PAGE_ID.replace("-", ""))
