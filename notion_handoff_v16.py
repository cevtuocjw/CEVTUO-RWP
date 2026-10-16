#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「v1.6 已发货」追加到 Notion「epub app 项目」页面。

沿用前面几版的做法(notion_handoff_v15 / _1016):**只追加,不改旧内容**。
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
B.append(h1("✅ v1.6 已发货 · 2026-10-16"))
B.append(para(rt("上面那三件事**全部处理完并发布了**。这一节是结果和当前状态。",
                 bold=True)))

B.append(h2("状态"))
B.append(bullet(rt("版本 v1.6;本机 app /Applications/CEVTUO-RWP.app(APP_VERSION = \"1.6\")")))
B.append(bullet(rt("Release v1.6,draft=false,就是 latest")))
B.append(bullet(rt("资产 macOS 106612111 / Windows 39634342 —— "
                   "本地 / Release / latest-download **三处逐位相同**")))
B.append(bullet(rt("仓库已推 main(f880e01)")))
B.append(bullet(rt("分发页**不用改** —— 页面上没有任何写死的版本号,"
                   "链接直指 releases/latest/download/(已核,不是假设)")))

B.append(h2("① 「新建合集点确定没反应」"))
B.append(callout("⭐ 这一轮找到了**上一轮没有的硬证据**:\n"
                 "用户真的试过(截图里填着 Cev1-101626),"
                 "而**用户库里根本没有那个合集**\n"
                 "⇒ POST /api/collection/create **没落地**。\n\n"
                 "也就是说:请求**真的失败了**,而界面上什么都没发生。",
                 "🔎", "blue_background"))
B.append(para(rt("⚠️ 但**复现不出来**(四种条件都试过,全正常),所以这一版修的"
                 "不是那个未知的触发条件,而是**「为什么它无声无息」**。", bold=True)))
B.append(para(rt("以前三条静默的路 ⇒ 现在都堵上了:")))
B.append(bullet(rt("失败只闪一个 **2.8 秒** toast ⇒ 错误**钉在弹窗里**,不自己消失")))
B.append(bullet(rt("慢请求和死按钮长得一模一样(disabled 只把不透明度降到 .45)"
                   "⇒ 按钮显示「**处理中…**」")))
B.append(bullet(rt("setView 裸调 async 渲染函数 ⇒ 抛了就是「无人处理的 promise 拒绝」,"
                   "页面上零痕迹;而 closeModal() 排在前面 ⇒ 连弹窗都关了"
                   "⇒ setView 兜住 + 退回列表 + **两条全局兜底**"
                   "(unhandledrejection / error)")))
B.append(bullet(rt("服务端 **log_message 是空的** —— 每个请求一个字都不记"
                   "⇒ 写请求/出错请求记进 logs/requests.log,**异常带栈**")))
B.append(callout("⭐⭐ 这次最值钱的一点:\n"
                 "「请求没到」和「到了但炸了」以前在**现场长得一模一样**,只能靠猜\n"
                 "—— 这就是查了两轮没结论的根子。", "💡", "green_background"))

B.append(h3("还欠用户的三个问题(下次先问,别猜)"))
B.append(num(rt("点下去**有没有一闪而过的提示**?那句话写着什么 —— 那句话直接就是答案")))
B.append(num(rt("是**每次都点不动**,还是**只某一次**?")))
B.append(num(rt("当时后台有没有在跑任务(导入 / 导出 / 生成)?")))
B.append(para(rt("⇒ 让用户看 ~/Library/Application Support/CEVTUO-RWP/logs/requests.log:"
                 "下一次如果那个 POST 根本没出现就是**请求没到**;"
                 "出现了而状态码是 400,那一行后面就带着 err= 和栈。")))

B.append(h2("② 章节显示时间和来源 —— 两处都做了"))
B.append(bullet(rt("界面(loadChapters):每行 「来源域名 · 日期 · 字数 · 导入方式」")))
B.append(bullet(rt("电子书(export_epub.build):章节页眉 "
                   "「来源 · 日期 · 原文链接」,和每日书对齐")))
B.append(callout("⚠️ 「来源」= **url 的域名**(srcHost())\n"
                 "**不是**库里那个 source 字段 —— 那是 urls/website/rss/markdown,"
                 "**是导入方式**。\n没有 url 的页面不显示来源,不编。", "⚠️"))
B.append(para(rt("新增 **BOOK_FORMAT_VERSION = 2**,写进 EPUB 的 OPF"
                 "(<meta name=\"cevtuo-format\" content=\"2\"/>)"
                 "—— 「这本书是哪一版做的」**查得到**。")))
B.append(para(rt("⚠️ 改了章节页眉/目录/扉页的形状就 +1;只改正文样式不用动。",
                 bold=True)))

B.append(h2("③ 这一版验过什么"))
B.append(bullet(rt("新增 verify-collection-create.py —— **17 条全绿**")))
B.append(bullet(rt("新增 negctl-collection-create.py —— **3/3 真的会红**,"
                   "还原逐字节一致")))
B.append(bullet(rt("回归 ui-refresh / bg-cover / job-skip / import-speed / "
                   "capture-challenge —— **40/40 全绿**")))
B.append(bullet(rt("DMG 挂起来看里面 —— APP_VERSION = \"1.6\",新代码都在包里")))

B.append(h3("顺手修掉两个「测试自己坏了」"))
B.append(para(rt("两个都红得**像产品有问题**,其实不是:")))
B.append(num(rt("verify-bg-cover.py 里写死 http://127.0.0.1:8791/bgtest.jpg —— "
                "那是开发时临时起过的 HTTP 服务的端口,而且**提交进了仓库**"
                "⇒ 换台机器必然红。改成夹具自己起 ThreadingTCPServer 发那张图。")))
B.append(num(rt("同文件「app 上确实挂上了自定义背景」直接读 has-bg,"
                "而**没人在这个测试里设过背景** ⇒ 用户机器上碰巧有 ⇒ 绿;"
                "沙盒/新机器 ⇒ 红。改成夹具自己先把背景设上,"
                "这条才是在测**产品**。")))

B.append(h3("跑验证器的新规矩"))
B.append(bullet(rt("两个 verify-*.py **不再无条件 capture.kill_chrome()** —— "
                   "那会杀掉用户正在用的采集浏览器;改成**先复用**")))
B.append(bullet(rt("地址可以用 **RWP_BASE** 指到沙盒实例,"
                   "不必拿用户正在用的真实数据当靶子")))
B.append(code_block(
    "SB=/tmp/rwp-sandbox; rm -rf \"$SB\"; mkdir -p \"$SB/state\"\n"
    "cp \"$HOME/Library/Application Support/CEVTUO-RWP/state/cevtuo.db\" \"$SB/state/\"\n"
    "cd \"$HOME/Library/Application Support/RSSDailyEpub\"\n"
    "CEVTUO_DATA_DIR=\"$SB\" nohup ./.venv/bin/python ui/server.py --port 8791 \\\n"
    "  > /tmp/rwp-sandbox-server.log 2>&1 &\n\n"
    "RWP_BASE=http://127.0.0.1:8791 ./.venv/bin/python verify-collection-create.py",
    "bash"))
B.append(callout("⚠️⚠️ 数据目录是 ~/Library/Application Support/CEVTUO-RWP,"
                 "**不是源码目录**(paths.py:打包形态下数据外置)。\n"
                 "查用户的库要读**那一个**。\n\n"
                 "⚠️ 杀沙盒:**先 lsof -nP -iTCP:8791 -sTCP:LISTEN -t 认人再 kill <PID>**,"
                 "别 pkill -f \"ui/server.py\" —— 那会**连 8611 上用户正在用的那份一起端掉**。",
                 "🛑", "red_background"))

B.append(h2("📌 贴进新窗口的第一句话"))
B.append(code_block(
    "读 ~/Library/Application Support/RSSDailyEpub/HANDOFF.md,然后继续处理。\n"
    "先看最上面那节「v1.6 已发货(2026-10-16)」。",
    "plain text"))
B.append(para(rt("交接文档母版:~/Library/Application Support/RSSDailyEpub/HANDOFF.md")))

print(f"=== 共 {len(B)} 个 block ===")
append(B)
print("完成 → https://www.notion.so/" + PAGE_ID.replace("-", ""))
