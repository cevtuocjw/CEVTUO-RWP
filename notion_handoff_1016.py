#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「v1.4 已发布 + 分发页修好」追加到 Notion「epub app 项目」页面。"""
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
    return {"object": "block", "type": "heading_1",
            "heading_1": {"rich_text": [rt(s)]}}


def h2(s):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [rt(s)]}}


def h3(s):
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [rt(s)]}}


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
    for i in range(0, len(blocks), 40):
        api("PATCH", f"/blocks/{PAGE_ID}/children",
            {"children": blocks[i:i + 40]})
        time.sleep(0.4)
        print(f"    已写入 {min(i + 40, len(blocks))}/{len(blocks)}")




B = []
B.append(divider())
B.append(h1("⏭ 下手要做的三件事 · 2026-10-16(交接)"))
B.append(para(rt("用户原话:「我今天处理书的时候想要做新合集点击确定点不动做不了新书 / "
                 "另外做合集的电子书没有显示时间和来源,你应该跟 daily rss 那边一样"
                 "每个章节都能展示这些信息才行 / 尽快处理完分发好到分发网站」", bold=True)))

B.append(h2("① 「新建合集点确定没反应」"))
B.append(para(rt("⚠️ 我已经查过,**复现不出来** —— 别再从头试一遍:", bold=True)))
B.append(bullet(rt("干净状态点一次 ⇒ ✅ 正常(弹窗关、视图切进去、卡片 0→2)")))
B.append(bullet(rt("设了自定义背景 ⇒ ✅ 正常(新加的两层都是 pointer-events:none,不挡)")))
B.append(bullet(rt("标题重名再建 ⇒ ✅ 正常(表里没有 title 唯一约束,重名随便建)")))
B.append(bullet(rt("任务跑过之后 ⇒ ✅ 正常")))
B.append(para(rt("按钮本身也没问题:量过它的 elementFromPoint,命中的就是它自己"
                 "(covered:false、disabled:false)。")))

B.append(h3("⚠️ 但找到一条结构性的脆弱,很可能就是真凶"))
B.append(para(rt("newCollectionModal()(ui/index.html 832 行)的收尾:")))
B.append(code_block("const c = await post('/api/collection/create', {…});\n"
                    "closeModal(); setView('collection', c.id);", "javascript"))
B.append(para(rt("而 api()(724 行)**只在 !r.ok && j.err 时才抛**;"
                 "renderCollection(cid) 第一句是 await get('/api/collection?id='+cid),"
                 "接口**查不到时返回 404**。")))
B.append(callout("⇒ **只要这一跳里任何一步失败,弹窗已经先关了,屏幕上什么都不会发生。**\n"
                 "用户看到的就是「点确定点不动」。\n\n"
                 "这是**静默死亡**,不是按钮坏了。", "⚠️", "red_background"))
B.append(para(rt("要改的方向(按重要性):", bold=True)))
B.append(num(rt("把 closeModal() 挪到成功之后 —— 失败了弹窗要留在原地、并把错误显示在弹窗里")))
B.append(num(rt("renderCollection 在拿不到合集时**出一句人话**,不要 c.title 直接 TypeError")))
B.append(num(rt("给 setView('collection', id) 加兜底:id 无效就退回合集列表并提示")))
B.append(para(rt("还需要的现场信息(下次先问用户,别猜):点下去**有没有一闪而过的提示**?"
                 "是每次都点不动还是只某一次?当时后台有没有在跑任务?")))

B.append(h2("② 「合集电子书没显示时间和来源,要像 daily rss 那样每章都展示」"))
B.append(para(rt("两边现在差在哪:", bold=True)))
B.append(bullet(rt("每日书**按来源分组**:rss2epub.py 702/763 行 "
                   "by_feed.setdefault(a.feed_title, …)")))
B.append(bullet(rt("每日书有日期:date_str = now.strftime(\"%Y-%m-%d\"),557 行")))
B.append(bullet(rt("合集这边:loadChapters()(~848 行)只显示 body_len · url/source · mode")))
B.append(para(rt("**数据其实已经有了,只是没展示** —— "
                 "/api/articles?collection= 每条都回 created_at 和 url。", bold=True)))
B.append(bullet(rt("**时间** ⇒ created_at(已有)")))
B.append(bullet(rt("**来源** ⇒ 从 url 取域名(new URL(u).hostname)。"
                   "⚠️ 别用 `source` 字段 —— 那是 urls/website/rss/markdown,"
                   "**是导入方式不是来源**")))
B.append(para(rt("要做的两处(用户说「每个章节都能展示」⇒ 界面和电子书里都要):")))
B.append(num(rt("界面:loadChapters() 那行 <small> 里加上 created_at 和域名")))
B.append(num(rt("电子书:导出的 EPUB 每章也带上(章标题下或导航条里)。"
                "⚠️ 改导出产物形状要动版本号,别让老书新书长得不一样还没人知道")))

B.append(h2("③ 处理完发布"))
B.append(callout("⚠️⚠️ **这个 shell 现在必须走代理**。\n"
                 "env -u HTTPS_PROXY … git push 会**必然** 75 秒超时 —— "
                 "直连 github.com 是被黑洞掉的,代理才通。\n\n"
                 "✅ git -c http.version=HTTP/1.1 push origin main  (保留代理环境变量)", 
                 "🔌", "yellow_background"))
B.append(num(rt("bash build-macos.sh && bash build-windows.sh")))
B.append(num(rt("git add <改过的文件>(别 -A)→ git commit")))
B.append(num(rt("git -c http.version=HTTP/1.1 push origin main")))
B.append(num(rt("gh release create v1.6 dist/… --latest")))
B.append(num(rt("验:draft=false + **本地/Release/latest 三处 content-length 逐位相同**")))

B.append(h2("📌 贴进新窗口的第一句话"))
B.append(code_block(
    "读 ~/Library/Application Support/RSSDailyEpub/HANDOFF.md,然后继续处理。\n"
    "先看最上面那节「下手要做的三件事(2026-10-16)」,里面写了已经查到哪一步。",
    "plain text"))
B.append(para(rt("交接文档母版:~/Library/Application Support/RSSDailyEpub/HANDOFF.md")))

print(f"=== 共 {len(B)} 个 block ===")
append(B)
print("完成 → https://www.notion.so/" + PAGE_ID.replace("-", ""))
