#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把交接文档追加到 Notion 的「epub app 项目」页面。

⚠️ 只**追加**,不删已有内容 —— 那个页面上已经有项目记录了。
⚠️ Notion 单次请求最多 100 个 block,数组项上限 100,富文本单条 ≤2000 字符。
   这里按 50 一批发,并且每段都短于 2000。
"""
import json
import os
import ssl
import sys
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


def rt(s, bold=False, code=False, link=None, color=None):
    o = {"type": "text", "text": {"content": s[:1900]}}
    if link:
        o["text"]["link"] = {"url": link}
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
    body = list(p) or [rt("")]
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": body}}


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


def todo(s, checked=False):
    return {"object": "block", "type": "to_do",
            "to_do": {"rich_text": [rt(s)], "checked": checked}}


def toggle(s, children=None):
    b = {"object": "block", "type": "toggle",
         "toggle": {"rich_text": [rt(s)]}}
    if children:
        b["toggle"]["children"] = children
    return b


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
        api("PATCH", f"/blocks/{PAGE_ID}/children", {"children": blocks[i:i + 50]})
        time.sleep(0.4)
        print(f"    已写入 {min(i + 50, len(blocks))}/{len(blocks)}")


B = []
B.append(divider())
B.append(h1("🤝 交接文档 · v1.3"))
B.append(para(rt("给下一个对话窗口看的。读完这一节就够了,不需要重新摸一遍代码。",
                 bold=True)))
B.append(para(rt("版本 v1.3 · 2026-09-30 · author: cevtuo")))

# ---------------------------------------------------------------- 一句话
B.append(h2("一句话"))
B.append(para(rt("把网页 / 站内链接 / RSS / Markdown 做成 EPUB 的 macOS + Windows 桌面应用。"
                 "原生窗口(AppKit + WKWebView)+ 本地 Python 服务,"
                 "运行时全部打包在内,用户开箱即用。")))

# ---------------------------------------------------------------- 路径
B.append(h2("一、东西都在哪(最容易搞错的地方)"))
B.append(bullet(rt("源码(唯一真相)  ", bold=True),
                rt("~/Library/Application Support/RSSDailyEpub —— 改代码只改这里")))
B.append(bullet(rt("构建/发布目录  ", bold=True),
                rt("~/Library/Application Support/CEVTUO-RWP-build —— 仓库工作副本,"
                   "docs/ windows/ usersite/ 在这")))
B.append(bullet(rt("数据目录  ", bold=True),
                rt("~/Library/Application Support/CEVTUO-RWP —— 5.9G,"
                   "用户的真实数据(书/历史/state)")))
B.append(bullet(rt("装好的 app  ", bold=True), rt("/Applications/CEVTUO-RWP.app")))
B.append(bullet(rt("仓库  ", bold=True),
                rt("cevtuocjw/CEVTUO-RWP", link="https://github.com/cevtuocjw/CEVTUO-RWP")))
B.append(bullet(rt("发布页  ", bold=True),
                rt("http://apps.cevtuogrnd.com/CEVTUO-RWP/",
                   link="http://apps.cevtuogrnd.com/CEVTUO-RWP/")))
B.append(bullet(rt("入口页  ", bold=True),
                rt("http://apps.cevtuogrnd.com/ —— 在另一个仓库 cevtuocjw.github.io")))
B.append(callout("usersite/ 是 cevtuocjw.github.io 的独立 git 仓库,嵌在 build 目录里。"
                 "已被主仓库 .gitignore 排除 —— 别再 git add -A 把它带进去,"
                 "会导致 GitHub Pages 构建直接失败。"));

# ---------------------------------------------------------------- 构建
B.append(h2("二、改完代码怎么落地(一条命令)"))
B.append(code_block('bash "~/Library/Application Support/CEVTUO-RWP-build/build-macos.sh"',
                    "bash"))
B.append(para(rt("它做六件事,顺序不能变:")))
B.append(num(rt("从源码目录 rsync 全部顶层 *.py + ui/ 进 .app 的 Contents/Resources/app/")))
B.append(num(rt("清 __pycache__")))
B.append(num(rt("清理旧名字残留")))
B.append(num(rt("3.5 重新编译 main.swift(swiftc,只有变了才编)")))
B.append(num(rt("3.8 自检:真的 import 一遍所有模块,缺模块当场失败")))
B.append(num(rt("codesign --force --deep --sign - → 打 DMG")))
B.append(para(rt("Windows 包:", bold=True), rt(" bash build-windows.sh(只换源码,不重拉依赖)")))
B.append(callout("这个脚本踩过三个大坑,每个都是「构建全绿但东西是坏的」:\n"
                 "① 文件清单写死 → 新加的 coverart.py 不在清单里 → app 一启动就 "
                 "ModuleNotFoundError,而构建全绿。现改成 ls *.py 自动取。\n"
                 "② 从不编译 main.swift → 改了 Swift 二进制没变 → WKWebView 的对话框/"
                 "文件框全失效,且无任何报错。现加了编译步骤。\n"
                 "③ rsync 保留 mtime → 旧 .pyc 被继续用 → 改了代码行为没变,完全不报错。"
                 "现每次清 __pycache__。\n\n"
                 "改完一定要跑,跑完一定要真的启动 app 看一眼。光看构建输出什么都说明不了。"))

# ---------------------------------------------------------------- 架构
B.append(h2("三、架构"))
for name, desc in [
    ("main.swift", "AppKit 外壳:开原生窗口(WKWebView)、拉起本地 Python 服务、退出时收掉"),
    ("ui/server.py", "本地 HTTP 服务 + 全部 API(127.0.0.1:8611)"),
    ("ui/index.html", "整个界面(单文件 SPA,内联 CSS/JS,中英双语)"),
    ("store.py", "数据层:sqlite3,collections / articles / rss_feeds / settings"),
    ("capture.py", "核心:Chrome profile 副本 + CDP 渲染取全文"),
    ("imports.py", "四条导入流水线(网址/站内链接/RSS/Markdown)"),
    ("extract_page.py", "网页 → 正文 / 链接 / sitemap"),
    ("export_epub.py", "合集 → EPUB(图片并发下载 + 内嵌)"),
    ("rss2epub.py", "RSS 每日书引擎(LinearBook)"),
    ("fulltext.py", "RSS 的四层全文策略"),
    ("coverart.py", "封面绘制(书名/作者压在图上;没封面就生成)"),
    ("scheduler.py", "launchd 定时任务管理"),
    ("run_scheduled.py", "定时入口(--ask = 先弹框问用户)"),
    ("jobs.py / paths.py", "任务进度 / 目录解析(程序目录 vs 数据目录分离)"),
]:
    B.append(bullet(rt(name + "  ", bold=True, code=True), rt(desc)))
B.append(para(rt("界面文案在 ui/index.html 的 I18N = {zh:{…}, en:{…}} 两张表里,"
                 "t('key') 取。", bold=True), rt("加文案必须两张表都加。")))

# ---------------------------------------------------------------- 坑
B.append(h2("四、必须记住的坑(都会复发)"))

B.append(h3("1. WKWebView 默认不实现 JS 对话框,也不弹文件框"))
B.append(para(rt("main.swift 里必须实现这四个 UIDelegate 方法,缺一个就有一类功能静默失效:")))
B.append(bullet(rt("runJavaScriptConfirmPanelWithMessage", code=True),
                rt(" —— 缺了 confirm() 直接返回 false,于是每处 "
                   "if(!confirm(...)) return; 永远提前返回。"
                   "症状:删合集/删章节/删书点了完全没反应,控制台一个错都没有。")))
B.append(bullet(rt("runOpenPanelWith", code=True),
                rt(" —— 缺了 <input type=\"file\"> 永远不弹框(上传封面点了没动静)")))
B.append(bullet(rt("runJavaScriptAlertPanelWithMessage", code=True), rt(" / "),
                rt("runJavaScriptTextInputPanelWithPrompt", code=True)))
B.append(callout("这类 bug 用浏览器或 Playwright 都测不出来,只有 WKWebView 会这样。"))

B.append(h3("2. 界面是 http://127.0.0.1:8611,所以不能用 file:// 加载本地资源"))
B.append(para(rt("浏览器会直接拦掉 http 页面里的 file:// 子资源。"
                 "症状:封面上传成功、接口全 200、但一片空白。"
                 "正确做法:走 /api/cover?id=<cid> 用 HTTP 发图。")))

B.append(h3("3. ui/server.py 里有一个 def log(*a)"))
B.append(para(rt("别再加叫 log 的 logger,会被它覆盖。用 slog。"
                 "覆盖的后果很阴:_delete_book 结尾抛 AttributeError → "
                 "文件已经删掉了却回报失败;_write_index 里被 except 吞掉 → "
                 "索引永远写不进去且毫无提示。")))

B.append(h3("4. 字数只有一个实现:store.plain_len"))
B.append(para(rt("剥标签 → 还原 HTML 实体 → 空白折叠成一个空格 → len()。"
                 "sanitize.plain_len 直接委托给它。别在别处再写一套 —— "
                 "曾经两套算法让同一篇文章显示 127 和 109 两个数。"
                 "也不能用 LENGTH(parsed_html) 冒充「字数」,那是 HTML 长度。")))

B.append(h3("5. 浏览器错误页是一次「成功的抓取」"))
B.append(para(rt("HTTP 200、结构完整、长度也够,状态码和长度都拦不住它。"
                 "识别用正文文字量(错误页几十到几百字),不能用页面字节数"
                 "(纯文字文章页只有 5KB)。实现在 imports.browser_error_page()。")))

B.append(h3("6. 标题别重复"))
B.append(para(rt("export_epub.build() 会在正文前加 <h2>标题</h2>,而抽取器"
                 "(trafilatura)经常已经把页面 <h1> 收进正文了 → 阅读器里标题出现两次。"
                 "_strip_dup_title() 负责去重,只在文字确实与标题相同时才删。")))

B.append(h3("7. 仓库里不能有嵌套 git 仓库"))
B.append(para(rt("usersite/ 那种自带 .git 的目录被 git add -A 带进来 → "
                 "GitHub Pages 构建直接失败,只回一句 Page build failed. duration 0,"
                 "零细节可查。排查:拿失败的 commit 和上一次成功的 commit 比 "
                 "git diff --name-only。")))

B.append(h3("8. launchd 任务的 label 是 com.cevtuo.rwp"))
B.append(para(rt("历史上有 com.cevtuo.rwp2epub / com.rssdaily.epub。"
                 "scheduler.unload_all() 会把旧的一起清掉 —— "
                 "光 unload 不够,plist 文件还在的话下次登录会自己回来,必须删文件。")))

# ---------------------------------------------------------------- 定时
B.append(h2("五、定时机制(v1.3 改过,别改回去)"))
B.append(bullet(rt("每天 09:00 和 15:00 各弹一次原生询问框", bold=True),
                rt(",点「开始下载」才真的下,点「今天不用」就跳过。"
                   "没人理 5 分钟自动当作不用。")))
B.append(bullet(rt("框里会写明今天是否已经下载过、几点、几本",
                   bold=True), rt("(state/rss_runs.json)")))
B.append(bullet(rt("软件没打开也会问", bold=True),
                rt(" —— 叫醒它的是 launchd(系统),不是用户点开 app。"
                   "只有三个边界:Mac 在睡觉(唤醒后补跑)、完全注销登录(不跑)、"
                   "首次可能要在「系统设置 → 通知」允许一次。")))
B.append(bullet(rt("RunAtLoad 是 false"), rt("(不是开机就跑)")))
B.append(bullet(rt("plist 里 StartCalendarInterval 是数组", bold=True),
                rt("(一天多个时刻)。不能写两个同名 <key> —— "
                   "plist 里后者覆盖前者,只会剩下午那次。")))

# ---------------------------------------------------------------- 发布
B.append(h2("六、发布流程"))
B.append(code_block(
    "cd \"~/Library/Application Support/CEVTUO-RWP-build\"\n"
    "git pull --rebase origin main      # 远端有定时 Actions 在写数据,先 rebase\n"
    "git push origin main\n"
    "cd usersite && git pull --rebase origin main && git push origin main\n"
    "gh release create v1.x dist/CEVTUO-RWP-macOS.dmg dist/CEVTUO-RWP-windows.zip \\\n"
    "  --repo cevtuocjw/CEVTUO-RWP --title \"...\" --notes \"...\"", "bash"))
B.append(para(rt("发布前必查:", bold=True)))
B.append(bullet(rt("gh release view <tag> --json assets", code=True),
                rt(" —— 资产文件名必须和页面里的链接一致"
                   "(页面用 releases/latest/download/<文件名>,发新版不用改页面)")))
B.append(bullet(rt("gh api repos/cevtuocjw/CEVTUO-RWP/pages/builds/latest", code=True),
                rt(" —— Pages 构建状态")))
B.append(callout("gh release create 有时会建出 Draft,而 Draft 不会成为 latest,"
                 "页面上的下载链接就会 404。检查 .draft,必要时 PATCH 成 draft=false。\n"
                 "Pages 构建偶尔长时间卡在 building;线上抓到旧内容时先看构建状态,"
                 "别急着改东西。", "⚠️"))

# ---------------------------------------------------------------- 分发页
B.append(h2("七、分发页(build/docs/index.html)"))
B.append(para(rt("用户明确要求过的版式,下次做同类页面直接照这个模式:")))
B.append(num(rt("Hero", bold=True), rt(" —— logo、标题、一句话、两个下载按钮、"
                                       "国内镜像、首次打开提示")))
B.append(num(rt("6 个功能卡片")))
B.append(num(rt("截图区 3~5 张", bold=True), rt("(硬上限),第一张 figure.wide 横跨整行,"
                                              "中英双语图注")))
B.append(num(rt("演示视频", bold=True), rt(" —— Playwright 录的 webm,"
                                           "controls preload=\"metadata\" + poster")))
B.append(num(rt("安装说明", bold=True), rt(" —— 必须包在 <details class=\"fold\"> 里默认折叠")))
B.append(num(rt("footer", bold=True), rt(" —— 玫瑰 logo + CEVTUO CLVNCHET / CEVTUOGRND")))
B.append(h3("截图/视频怎么做(别再试 AppleScript 点窗口)"))
B.append(callout("不要去点 app 的 WKWebView 窗口 —— CDP 连不上 WKWebView,"
                 "只能系统级点击,而窗口位置会漂。我为此连续三轮坐标都对不准。\n\n"
                 "正确做法:Playwright 打开 http://127.0.0.1:8611,用 JS 精确驱动。"
                 "同一份 HTML/CSS,视口 1380×900 @2x。视频就是同一个 context 的 "
                 "recordVideo。\n\n"
                 "必须用 Playwright 自带的 chromium —— 本机 Chrome 153 对页面做"
                 "自动暗色反色,截出来的是假图。", "📸", "blue_background"))

# ---------------------------------------------------------------- 状态
B.append(h2("八、当前状态 / 没做的事"))
B.append(h3("已完成并实测"))
for x in [
    "四条导入路径、无页数上限导出、RSS 每日书 + 按需询问式定时",
    "章节管理:每章可「再次获取」,抓取失败/内容不对的在行内标原因",
    "导出后给图片下载账单(几张成功、哪几张没下到 + 原因)+ 一键重取",
    "封面:给链接抓图(og:image 优先)或关键词搜索,选中后自动压上书名作者;"
    "没封面就生成一张只有书名作者的",
    "书库:卡片上一键清空整组、按月归档、外部挪走文件后卡片跟着消失",
    "新 logo(银印章 + 花头)已铺到 app 图标 / 界面 / 分发页 / 入口页 / Windows 包",
]:
    B.append(bullet(rt(x)))
B.append(h3("已知问题"))
B.append(todo("https://apps.cevtuogrnd.com 打不开(只有 HTTP)。"
              "cevtuocjw.github.io 的 Pages https_certificate 是 null,"
              "API 回「The certificate does not exist yet」。缺 GitHub 的 TXT 校验记录,"
              "而那个值只在网页 Settings → Pages 里显示,API 读不到 —— 需要用户自己复制。"))
B.append(todo("Windows 包从未在真实 Windows 机器上跑过。定时功能在 Windows 不可用"
              "(需另接计划任务),界面上的「一键生成」正常。"))
B.append(callout("logo 素材来自 Pinterest(@genias.art 的作品),原图写着未经书面许可"
                 "禁止使用。已按用户要求裁掉版权声明。已明确告知用户,用户决定继续用 —— "
                 "但别再主动扩大使用范围。", "⚖️", "red_background"))

# ---------------------------------------------------------------- 验证
B.append(h2("九、验证的规矩(这个项目的头号教训)"))
B.append(para(rt("「构建通过」什么都说明不了,「接口返回 200」也说明不了,截图也骗人。",
                 bold=True)))
B.append(para(rt("真实翻车记录:")))
for x in [
    "封面走 file:// → 接口全 200、上传成功、封面一片空白",
    "log 撞名 → 文件已经删掉了才抛异常,界面回报失败",
    "字数提示和章节列表同一件事两个数字(127 vs 109),两边都没报错",
    "我在 640px 缩略图上把「↑ ↓ ⟳」误读成 ✓/✗,差点去修一个不存在的 bug",
    "抹文字时,我拿一个固定半径的圆去框文字,而那个字伸到圈外 —— "
    "调了三次模糊半径都没用,真因是框小了",
]:
    B.append(bullet(rt(x)))
B.append(callout("唯一可靠的做法:真的把 app 跑起来 → 驱动真实页面 → 读 DOM → "
                 "亲眼看图(而且要放大)。", "✅", "green_background"))

# ---------------------------------------------------------------- 偏好
B.append(h2("十、用户偏好(别推翻)"))
for x in [
    "不做老页面重构;出错要可见(失败也要建占位章并写明原因),不要静默跳过",
    "失败清单要能一键重试(「专门把没获取到的再获取一遍」)",
    "界面中文为主,中英双语",
    "分发页安装说明折叠",
    "截图 3~5 张",
    "付费墙绕过扩展不打包进 DMG(程序只使用用户自己浏览器里已装的,"
    "靠 CDP Extensions.loadUnpacked 真正授权)",
    "多 URL / 站内链接 / RSS / Markdown 四条路都要在",
]:
    B.append(bullet(rt(x)))

print(f"=== 共 {len(B)} 个 block,开始写入页面 {PAGE_ID} ===")
append(B)
print("完成 → https://www.notion.so/" + PAGE_ID.replace("-", ""))
