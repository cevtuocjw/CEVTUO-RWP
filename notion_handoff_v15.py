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
B.append(h1("🚀 v1.5 已发布 · 2026-10-10 深夜"))

B.append(h2("① 「导入完的内容在章节管理里面没有看到」—— 数据是对的,界面是死的"))
B.append(para(rt("**不是导入失败** —— 文章确实写进库了。真因在前端的进度轮询:")))
B.append(code_block(
    "if(!host.querySelector('.prog')){ clearInterval(timer); return; }", "javascript"))
B.append(para(rt("你嫌慢、把进度窗一关(点关闭或按 Esc),这行就让**轮询当场停掉**,"
                 "于是「导入完成后刷新章节列表」那一步**永远不执行**。"
                 "全程不报错、进度条也确实跑完了 —— 界面上却还是旧列表,"
                 "写着「还没有章节」。")))
B.append(callout("已修:轮询和「进度窗还在不在」解耦。你随时关掉进度窗,"
                 "导入结束后章节列表**照样会自己刷新**。\n\n"
                 "⭐ 负向对照跑过:把这条改回去 ⇒ 界面 0 章 / 后端 2 条,原症状复现。",
                 "✅", "green_background"))

B.append(h2("② 「遇到卡壳的我可以中途跳过,确保一定做完,而不是只能取消」"))
B.append(para(rt("进度窗里新增 **「跳过这一章」** 按钮。", bold=True)))
B.append(bullet(rt("**跳过 ≠ 取消**:取消是整批不要了;跳过是**只丢这一条、后面的继续**。")))
B.append(bullet(rt("点一次跳掉**跑得最久的那一条**(卡住的那条正好就是它),再点跳下一个。")))
B.append(bullet(rt("被跳过的那一章留一个占位、写明「已跳过」,方便之后单独重抓。")))
B.append(para(rt("实测:一个永远加载不完的页面,整批 "), rt("33.2 秒 → 3.0 秒", bold=True),
              rt(" 跑完,其它链接照常导入。")))
B.append(callout("⚠️ 第一版这里挑错了对象:用 max(启动时间) 取到的是**最新开始**的那条 "
                 "⇒ 跳掉的是一个刚开始、本来完全正常的链接,而卡住的纹丝不动。\n\n"
                 "更阴的是它**看起来完全正确**:接口回 ok:true、日志写着「已跳过 …」、"
                 "进度也确实往前走 —— 只有把「跳的是哪一条」打出来才看得出来。\n\n"
                 "改成 min,并在验证脚本里钉了一条断言:「跳掉的必须是索引 0(卡住那条)」。",
                 "🎯", "red_background"))

B.append(h2("③ 可以换背景了"))
B.append(para(rt("**设置 → 背景图**:")))
B.append(bullet(rt("把**链接**粘进去它自己去取。给图片直链直接用;"
                   "给网页就把页面里够大的图列出来让你挑。")))
B.append(bullet(rt("也可以**选本机图片**。")))
B.append(bullet(rt("右侧有**预览**,看到的就是设完之后的样子。")))
B.append(bullet(rt("**恢复默认背景**一键还原。")))
B.append(para(rt("关于「铺满但不拉伸」:", bold=True),
              rt("用的是 "), rt("background-size: cover", code=True),
              rt(" —— 保持原图比例、铺满整屏、把溢出的边裁掉,**绝不会把图拉变形**。"
                 "存图时也是等比缩放,并且会按 EXIF 自动把手机拍歪的方向转正。")))
B.append(para(rt("像素验证(不是看计算样式,是数像素):", bold=True)))
B.append(bullet(rt("`100% 100%`(拉伸) ⇒ 量到 38×135,宽高比 "), rt("0.28", bold=True),
                rt("(正方形被压成细条)")))
B.append(bullet(rt("`cover`(被测) ⇒ 量到 134×134,宽高比 "), rt("1.00", bold=True),
                rt("(还是正方形)")))

B.append(h2("⚠️ 一条网络上的更正 —— 这个 shell 现在**必须走代理**"))
B.append(para(rt("今天排了很久才找到:**git 直连 github.com 被黑洞掉了**"
                 "(Trying 20.205.243.166:443 然后 75 秒超时),而**代理是通的**。")))
B.append(code_block(
    "\u2705 git -c http.version=HTTP/1.1 push origin main        # 带 env 里那个 7890 代理\n"
    "\u274c env -u HTTPS_PROXY … git push                        # 去掉代理 = 直连 = 必超时",
    "bash"))
B.append(callout("⚠️ 和此前记的(2026-10-03「走 7890 全连不上、不带代理反而通」)**正好相反**。"
                 "别再照那条把代理 unset 掉,会白等 75 秒 × N 次。\n\n"
                 "⚠️ 我还被 curl 骗过一次:curl 返回 200 看似「直连通了」—— "
                 "其实 curl 那次走的是代理,git 那次是直连。"
                 "**两条命令走的不是同一条路,拿一个的结论推另一个是这个坑的全部。**",
                 "🔌", "yellow_background"))

B.append(h2("当前状态"))
B.append(bullet(rt("本机 app  ", bold=True), rt("v1.5")))
B.append(bullet(rt("Release  ", bold=True), rt("✅ v1.5,draft=false,就是 latest")))
B.append(bullet(rt("分发页  ", bold=True),
                rt("✅ 不用改 —— 用的是 releases/latest/download/,自动跟随")))
B.append(bullet(rt("仓库  ", bold=True), rt("✅ 已推 "), rt("256dc61", code=True)))
B.append(para(rt("下载到的到底是不是新版 —— 本地包 / Release 资产 / latest 链接 "
                 "**三处 content-length 逐位相同**:macOS 103136247、Windows 39618966。",
                 bold=True)))

B.append(h2("今天的验证脚本(改这几处之前先跑)"))
B.append(bullet(rt("verify-bg-cover.py 11 条 / verify-capture-challenge.py 9 条 / "
                   "verify-job-skip.py 8 条 / verify-ui-refresh.py 7 条 / "
                   "verify-import-speed.py 5 条")))
B.append(para(rt("**五个共 40 条,全绿**;其中四个做过负向对照(把 bug 放回去 → 真的红)。")))

B.append(h2("📌 贴进新窗口的第一句话"))
B.append(code_block(
    "读 ~/Library/Application Support/RSSDailyEpub/HANDOFF.md,然后继续处理。\n"
    "v1.5 已经发布了(跳过卡住的章节 / 导入完界面一定刷新 / 可换背景图)。\n"
    "先看文档里的「当前状态」和「还没解决的」两节。", "plain text"))

print(f"=== 共 {len(B)} 个 block ===")
append(B)
print("完成 → https://www.notion.so/" + PAGE_ID.replace("-", ""))
