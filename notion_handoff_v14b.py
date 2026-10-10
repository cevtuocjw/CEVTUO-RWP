#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 v1.4 第二批(2026-10-10 晚)追加到 Notion「epub app 项目」页面。

只追加,不删已有内容。⚠️ Notion 单次 ≤100 block,这里按 40 一批发。
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
B.append(h1("🔧 v1.4 第二批 · 2026-10-10 晚"))
B.append(para(rt("用户原话:「"), rt("为什么现在导入合集过程变得这么慢,很多链接的时候"),
              rt("」/「"), rt("有时候总是跳出来弹窗为什么"),
              rt("」/「"), rt("如果卡壳了我就没有任何办法"),
              rt("」——三句都有对应的真 bug,而且没有一句是网络问题。", bold=True)))

# ------------------------------------------------------------------ ④
B.append(h2("④ 「很多链接的时候特别慢」"))
B.append(para(rt("根因:", bold=True),
              rt("import_links()", code=True),
              rt("(站点内链接 / sitemap 走的就是这条)**是逐个串行抓的**。"
                 "上一轮只改了 import_urls(),这条路一个都没动。")))
B.append(para(rt("修:", bold=True),
              rt("改成 ThreadPoolExecutor(max_workers=3),和 import_urls 同一套。")))
B.append(para(rt("实测:", bold=True),
              rt("6 个各 2 秒的页面 12.0s → 4.2s。", bold=True)))

# ------------------------------------------------------------------ ⑤
B.append(h2("⑤ ⭐⭐⭐ 「如果卡壳了我就没有任何办法」"))
B.append(para(rt("现象:", bold=True),
              rt("一个永远加载不完的页面,点取消之后整批**又等了 27.6 秒**。")))
B.append(para(rt("根因两层,第二层才是真凶:", bold=True)))
B.append(num(rt("import_links() 调 article_from_url() 时**根本没传 should_stop** "
                "⇒ 最底层压根不知道用户点了取消。")))
B.append(num(rt("⚠️ 真正的坑:", bold=True),
             rt("Cancelled 被 "), rt("except Exception", code=True),
             rt(" 吃掉了。Chrome 那边 2.5 秒就响应了取消并抛出,而 fetch_html() "
                "把它记成「Chrome 渲染失败」,**转身去试直连** —— 而 requests.get "
                "没有任何取消机制,又挂了 25 秒。")))
B.append(callout("取消被降级 = 取消失效。\n\n"
                 "修法:加一个专用异常 capture.Cancelled,并让 fetch_html 显式地 "
                 "except capture.Cancelled: raise —— 在其它 except Exception 之前拦住。\n\n"
                 "实测:同一个卡死页面 27.6s → 2.6s(超时设的是 45s)。",
                 "⭐", "green_background"))
B.append(para(rt("⚠️ 附带一条契约:", bold=True),
              rt("HTTP 那条路的取消**只能做到「不再开新的」**,在途的 requests.get "
                 "收不回来 —— 这不是 bug,是 requests 的能力边界。断言要按这个写"
                 "(第一版阈值写成「一个页面耗时之内返回」,2.0s vs 2.0s 红了一条,"
                 "**红的是断言写错了契约**)。")))

# ------------------------------------------------------------------ ⑥
B.append(h2("⑥ ⭐⭐⭐ 「总是跳出来弹窗」+「正文只有一百多字」—— 同一个根因"))
B.append(para(rt("这两句其实是一件事,而且", bold=True),
              rt("它不是付费墙", bold=True, color="red"), rt("。", bold=True)))
B.append(para(rt("FT 会先发一张 "), rt("Security Verification / 请稍候…", code=True),
              rt(" 的机器人校验中间页,十几秒后才换成真文章。这张中间页最坑的是:")))
B.append(bullet(rt("document.readyState === \"complete\"", code=True),
                rt(" ⇒ 「文档加载完了」成立")))
B.append(bullet(rt("innerText 恒为 554 字,**十几秒一个字都不变**", ),
                rt(" ⇒ 「正文稳定了」成立")))
B.append(para(rt("而 goto() 的收尾判据恰恰是「稳定 settle 秒 **且** readyState 完成」"
                 "——它全都满足,所以 1.2 秒就把这张纸收下来了。")))
B.append(callout("症状:章节标题「请稍候…」,正文「Financial Times Security Verification …」,"
                 "145 字,**全程没有任何报错**。\n\n"
                 "⭐ 这是 FACT 纪律 6/15 那个家族:判据测的不是它以为的东西。"
                 "「稳定」恰恰是中间页的特征,拿「稳定」当判据**必然中招**。",
                 "⚠️", "red_background"))
B.append(para(rt("修(两半,缺一不可):", bold=True)))
B.append(num(rt("capture._looks_interstitial()", code=True),
             rt(" —— 认得出这类页面(关键词 **+ 必须确实短**,只看关键词会误伤"
                "「讲 Cloudflare 的文章正文明明写着 Just a moment」)。"
                "goto() 认出它就**不要当成「稳定了」**,额外给 30 秒预算,"
                "并在放行后**把预算补回来**。")))
B.append(num(rt("imports.browser_error_page()", code=True),
             rt(" 也认它 —— 等够了还没放行就**老实报**「被网站的机器人校验挡住」,"
                "而不是把一张「请稍候…」当成一章正文。")))
B.append(callout("⚠️⚠️ 为什么不直接拿 FT 来验 —— 试过了,验不了。\n\n"
                 "FT 的校验是**间歇**的:清掉 cookie + localStorage "
                 "(Storage.clearDataForOrigin)之后**两组都直接拿到全文** —— 对照红不了,"
                 "看着像「这个检测没用」,其实只是**它这次没发作**。\n\n"
                 "⇒ 依赖被测方「按时发作」的对照,等于没对照。\n"
                 "所以改成自己起 ThreadingHTTPServer 重放那张中间页:机制是确定的,"
                 "断言就也是确定的。",
                 "🎯", "blue_background"))
B.append(para(rt("本地重放的结果(verify-capture-challenge.py 9/9):", bold=True)))
B.append(bullet(rt("关掉检测(负向对照) ⇒ ", bold=True), rt("325 字 / 2.5s", code=True),
                rt(" ← 线上那个 bug 原样复现")))
B.append(bullet(rt("开着检测 ⇒ ", bold=True), rt("1524 字 / 14.1s", code=True),
                rt(" ← 等过去了")))
B.append(bullet(rt("开着检测但永远不放行 ⇒ ", bold=True),
                rt("识别为「被机器人校验挡住」,**不当成正文**")))
B.append(callout("写这个验证脚本时**量具自己坏过两次**:\n\n"
                 "· 数「可见字数」的正则**没剔 <script>**,而夹具把整篇文章放在 <script> 里 "
                 "⇒ 量出 207452 字,看着像产品坏了\n"
                 "· why 是在 finally **恢复检测之后**才求值的,「关掉检测」那一臂的 why "
                 "依然是「被挡住」⇒ **对照根本没关成**",
                 "🔬", "orange_background"))

# ------------------------------------------------------------------ 端到端
B.append(h2("端到端实测(让 app 自己跑,不是探针)"))
B.append(para(rt("通过界面同一个接口 "), rt("POST /api/import/urls", code=True),
              rt(" 抓 FT,任务日志:")))
B.append(code_block("✓ US 30-year Treasury yield hits highest since 2002 (3293 字,chrome)",
                    "plain text"))
B.append(bullet(rt("3293 字 —— 全文,不是那张 145 字的纸")))
B.append(bullet(rt("自动化 Chrome 带 "), rt("--headless=new", code=True),
                rt(" ⇒ **没有窗口弹出来**")))
B.append(bullet(rt("机器上另有的 2 个可见 Chrome 窗口是用户自己的(不带 user-data-dir)")))

# ------------------------------------------------------------------ 状态
B.append(h2("当前状态"))
B.append(bullet(rt("本机 app  ", bold=True), rt("v1.4,已装、已运行")))
B.append(bullet(rt("DMG / Windows 包  ", bold=True),
                rt("✅ 都重建了(10:10,**两个都含今天全部修复**)")))
B.append(bullet(rt("Release  ", bold=True, color="red"),
                rt("⚠️ 还是 v1.3 —— v1.4 没发布")))
B.append(bullet(rt("分发页  ", bold=True, color="red"),
                rt("⚠️ 还是 v1.3 那版(页面用 releases/latest/download/,发布后自动跟随)")))
B.append(bullet(rt("仓库  ", bold=True, color="red"),
                rt("⚠️ v1.4 的改动还没提交")))

B.append(h2("今天新加的两个验证脚本(以后改这几处先跑它们)"))
B.append(bullet(rt("verify-capture-challenge.py  ", bold=True, code=True),
                rt("9 条 —— 机器人校验中间页")))
B.append(bullet(rt("verify-import-speed.py  ", bold=True, code=True),
                rt("5 条 —— 并行导入 + 取消真的能逃出来")))
B.append(para(rt("两个都自带负向对照,而且**都真的红过**"
                 "(325 字 / 2.5s;27.6s)。跑到全绿再提交。")))

B.append(h2("下次要做的事(按顺序)"))
B.append(num(rt("cd build && git add -A && git commit && git pull --rebase && git push")))
B.append(num(rt("gh release create v1.4 dist/CEVTUO-RWP-macOS.dmg "
                "dist/CEVTUO-RWP-windows.zip")))
B.append(num(rt("检查它**不是 Draft** —— Draft 不会成为 latest,下载链接会 404")))

B.append(h2("还没解决的"))
B.append(todo("https://apps.cevtuogrnd.com 打不开(缺 GitHub 的 TXT 校验记录,"
              "值只在网页 Settings → Pages 里显示)"))
B.append(todo("Windows 包从未在真实 Windows 上跑过"))
B.append(todo("Dock / Finder 图标可能还是旧的(文件是新的,IconServices 缓存)。"
              "已 killall + lsregister,**没有逐项验证过**,别当它已经好了"))

B.append(callout("⚠️ 一条要一直记着的:FT 的校验是**间歇**的。\n\n"
                 "别拿 FT 当回归用例 —— 它发作的时候才测得到,用它验等于把结论交给运气。"
                 "要验就走 verify-capture-challenge.py 的本地重放。",
                 "📌", "blue_background"))

B.append(h2("📌 贴进新窗口的第一句话"))
B.append(code_block(
    "读 ~/Library/Application Support/RSSDailyEpub/HANDOFF.md,然后继续处理。\n"
    "当前是 v1.4,已装在本机但还没发布(Release 上最新是 v1.3)。", "plain text"))

print(f"=== 共 {len(B)} 个 block ===")
append(B)
print("完成 → https://www.notion.so/" + PAGE_ID.replace("-", ""))
