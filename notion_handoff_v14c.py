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
B.append(h1("🚀 v1.4 已发布 · 2026-10-10 晚"))
B.append(para(rt("Release、分发页、仓库三处都到位了。", bold=True)))

# ---------------------------------------------------------------- 发布
B.append(h2("发布:"))
B.append(bullet(rt("Release  ", bold=True),
                rt("v1.4,", ), rt("draft=false", code=True), rt(",就是 "),
                rt("latest", code=True)))
B.append(bullet(rt("分发页  ", bold=True),
                rt("https://apps.cevtuogrnd.com/CEVTUO-RWP/", code=True)))
B.append(bullet(rt("仓库  ", bold=True),
                rt("CEVTUO-RWP ", bold=True), rt("56bf611", code=True),
                rt(";usersite ", bold=True), rt("d67b979", code=True)))

B.append(para(rt("⚠️ 发出去之前逐项验过(不是「命令跑完了」就算数):", bold=True)))
B.append(bullet(rt("Release 是不是 Draft ⇒ "), rt("draft=false", code=True)))
B.append(bullet(rt("是不是 latest ⇒ "), rt("gh api .../releases/latest", code=True),
                rt(" 回的就是 v1.4")))
B.append(bullet(rt("资产传完没有 ⇒ 两个都是 "), rt("state=uploaded", code=True)))
B.append(bullet(rt("下载链接通不通 ⇒ 页面里 **6 个全部 HTTP 200**"
                   "(GitHub 直链 + 3 个镜像)")))
B.append(bullet(rt("**下载到的到底是不是新版** ⇒ ", bold=True),
                rt("latest/download/CEVTUO-RWP-macOS.dmg", code=True),
                rt(" 的 content-length = "), rt("102168570", code=True),
                rt(",与本地 v1.4 包**逐位一致**(v1.3 是 94547711)")))
B.append(bullet(rt("Pages 构建 ⇒ "), rt("status=built", code=True),
                rt(",20954ms,无 error")))
B.append(bullet(rt("页面资源齐不齐 ⇒ 逐项核对 assets/* 6 个 + logo.png + bg.jpg,"
                   "**0 缺失**")))

# ---------------------------------------------------------------- 顺手修的
B.append(h2("⭐ 顺手修掉一个一直没发现的 bug:改名后入口一直 404"))
B.append(para(rt("usersite/index.html 链的是 "), rt("/CEVTUO-RWP/", code=True),
              rt(",而 usersite 里那个目录**还叫 "),
              rt("CEVTUO-RWP2EPUB/", code=True),
              rt("** —— 也就是说改名之后,apps.cevtuogrnd.com 上那个入口"
                 "**一直是 404 的**。")))
B.append(para(rt("更阴的是:旧目录里**确实有一个跳转桩**,注释写着"
                 "「软件已改名为 CEVTUO-RWP,正在跳转…」—— 而**跳转目标根本不存在**。"
                 "所以它 200 OK 地把你送进一个 404。")))
B.append(para(rt("修:在 usersite 里**新建 "), rt("CEVTUO-RWP/", code=True),
              rt("**,把 docs/index.html 连资源一起搬过去"
                 "(index.html + assets/ + logo.png + bg.jpg)。"
                 "旧目录保留成跳转桩,以前发出去的链接继续能用。")))
B.append(callout("教训:**「我改了 A」和「A 指向的东西存在」是两件事。**\n\n"
                 "跳转桩的注释明明写着「改名为 CEVTUO-RWP」,但没人验过目标目录是否存在。\n"
                 "判据要打在**跳转的终点**上,不是起点。",
                 "🎯", "blue_background"))

# ---------------------------------------------------------------- 已过期
B.append(h2("✅ 一条要更新的旧记录"))
B.append(para(rt("此前记的是「apps.cevtuogrnd.com 打不开(缺 GitHub TXT 校验记录)」。"
                 "**2026-10-10 实测已经通了**:"
                 "https://apps.cevtuogrnd.com/ → 200,HTTP 会 301 跳到 HTTPS。"
                 "**这条已经过期,别再按「打不开」处理。**")))

# ---------------------------------------------------------------- 还没解决
B.append(h2("还没解决的"))
B.append(todo("Windows 包从未在真实 Windows 上跑过"))
B.append(todo("Dock / Finder 里的图标可能还是旧的(文件是新的,IconServices 缓存)。"
              "已 killall + lsregister,**没有逐项验证过**,别当它已经好了"))
B.append(todo("定时任务(09:00 / 15:00 询问)**默认是关的**(mode: off)。"
              "之前要过这个功能,但发布时没开启 —— 要在设置里手动打开"))

# ---------------------------------------------------------------- 下次发版
B.append(h2("下次发版(照抄)"))
B.append(code_block(
    'cd "$HOME/Library/Application Support/CEVTUO-RWP-build"\n'
    "\n"
    "# 1. 构建(它会自己跑 typecheck、编译 main.swift、打 DMG,并把源码同步进仓库)\n"
    "bash build-macos.sh && bash build-windows.sh\n"
    "\n"
    "# 2. 提交 + 推  ⚠️ 别 git add -A\n"
    "git add <改过的文件> && git commit\n"
    "git pull --rebase origin main && git push origin main\n"
    "\n"
    "# 3. 发 Release(资产名必须和页面里的一致)\n"
    "gh release create v1.x dist/CEVTUO-RWP-macOS.dmg dist/CEVTUO-RWP-windows.zip \\\n"
    '  --repo cevtuocjw/CEVTUO-RWP --title "CEVTUO-RWP v1.x" --notes-file notes.md --latest\n'
    "\n"
    "# 4. ⚠️ 验它真成了 latest(别只看 create 没报错)\n"
    "gh api repos/cevtuocjw/CEVTUO-RWP/releases/latest \\\n"
    '  -q \'"tag=" + .tag_name, "draft=" + (.draft|tostring)\'\n'
    "\n"
    "# 5. 分发页不用改 —— 它用 releases/latest/download/,自动跟随\n"
    "# 6. 站点本身改了才动 usersite(独立仓库)\n"
    "cd usersite && git pull --rebase origin main && git push origin main",
    "bash"))

B.append(h2("📌 贴进新窗口的第一句话"))
B.append(code_block(
    "读 ~/Library/Application Support/RSSDailyEpub/HANDOFF.md,然后继续处理。\n"
    "v1.4 已经发布(Release / 分发页 / 仓库都到位了)。\n"
    "先看文档里的「当前状态」和「还没解决的」两节。", "plain text"))

print(f"=== 共 {len(B)} 个 block ===")
append(B)
print("完成 → https://www.notion.so/" + PAGE_ID.replace("-", ""))
