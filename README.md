# CEVTUO-RWP

把**网页、网站内链接、RSS 订阅、Markdown** 做成 EPUB 电子书。原生桌面应用，自带运行时，下载即用。

**author: cevtuo**

项目主页：https://cevtuocjw.github.io/CEVTUO-RWP/

---

## 下载

| 平台 | 文件 | 说明 |
|---|---|---|
| macOS | `CEVTUO-RWP-macOS.dmg` | Apple Silicon · 约 100 MB |
| Windows | `CEVTUO-RWP-windows.zip` | Windows 10/11 x64 · 约 37 MB |

在 [Releases](../../releases/latest) 页下载。

> **macOS 首次打开**：ad-hoc 签名，未经 Apple 公证，会被 Gatekeeper 拦。请**右键 → 打开**，
> 或执行 `xattr -dr com.apple.quarantine /Applications/CEVTUO-RWP.app`。
>
> **Windows**：解压后双击 `CEVTUO-RWP.cmd`。

**安装包自带 Python 运行时和全部依赖** —— 目标机器不需要装 Python、不需要 pip、不需要 venv。

---

## 功能

### CEVTUO合集坊
一个合集 = 一本电子书，每篇文章 = 一个章节。

- **多个网址** —— 一行一个，批量抓取
- **网站内链接** —— 给一个站点，列出页面里的链接或 `sitemap.xml`，勾选后批量导入
- **RSS 订阅** —— 给一个 feed，列出条目勾选导入
- **Markdown** —— 用 `# 一级标题` 分篇
- **章节管理** —— 排序、删除、空白页、随时从网址补一页
- **逐页查看与编辑** —— 预览 / 所见即所得 / HTML 源码三种模式
- **导出 EPUB** —— 封面、作者、语言、前言目录、图片内嵌
- **没有页数限制**

### Rssdailyepub
- 一键生成每日书，选几天内、每源几条、哪些分类，全程有进度
- 定时：每天 / 每隔两天 / 每隔三天 / 取消，时间可改
- RSS 源管理：分类与源的增删改、导入 OPML

> 可自行添加或者更换修改自己的 rss 实例。
> 已打包我的日常关注 rss 进入扩展作为默认例子。

### 书库
- 合集电子书按合集分文件夹
- 每日书按日期分文件夹，点进去看当天的每一本
- **书库位置可改**，改的时候已生成的书会一起搬过去

---

## 全文获取是怎么做到的

程序会**复制一份你自己浏览器的 profile**（连同你装好的扩展和登录态），用这份副本启动一个带调试端口的浏览器，通过 CDP 打开页面、等 JavaScript 渲染完、读回 DOM。

这样做而不是用普通 HTTP 抓取，是因为：

- 很多站点的正文要靠 JavaScript 才出现
- 你如果装了任何解锁 / 增强类扩展，那是**扩展在浏览器里当场起作用**的，导出几个 Cookie 复制不了这个效果
- Chrome 136 起禁止对默认用户目录开调试端口，必须换一个 user-data-dir

支持自动探测：**Chrome / Edge / Brave / Arc / Vivaldi / Chromium / Opera**。

正文抽取走三级兜底：trafilatura → 最大文本块 → 整页 body，保证永远有东西可读。

---

## 从源码运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install feedparser ebooklib trafilatura requests pillow websocket-client
python ui/server.py --open
```

macOS 原生窗口外壳是 `main.swift`（需要 Xcode 命令行工具）：

```bash
swiftc -O -target arm64-apple-macosx11.0 -o main main.swift -framework Cocoa -framework WebKit
```

## 构建

- **macOS**：`.app` 里放 `python/`（可搬运的 CPython）+ `pylibs/`（依赖）+ 源码，
  `codesign --force --deep --sign -` 后 `hdiutil create` 成 DMG
- **Windows**：见 `ci/build-windows.yml`。Windows 包**必须在 Windows 上构建或用交叉解析**——
  Python 运行时与 lxml / Pillow 都是平台相关的二进制

## 目录

| 文件 | 作用 |
|---|---|
| `capture.py` | 核心：浏览器 profile 副本 + CDP 渲染 |
| `extract_page.py` | 网页 → 正文 / 链接 / sitemap / Markdown |
| `export_epub.py` | 合集 → EPUB |
| `imports.py` | 四条导入流水线 |
| `store.py` `paths.py` | 数据层与目录解析（程序目录 / 数据目录分离） |
| `jobs.py` `scheduler.py` | 任务进度与定时 |
| `rss2epub.py` `fulltext.py` | RSS 连续流引擎与四层全文策略 |
| `ui/` | 界面与接口 |

## 数据位置

- macOS：`~/Library/Application Support/CEVTUO-RWP`
- Windows：`%LOCALAPPDATA%\CEVTUO-RWP`

里面有 `output/`（每日书）、`books/`（合集导出）、`history/`、`state/`、`logs/`。

---

## 已知限制

- **Windows 版的定时功能暂不可用**。macOS 用 launchd，Windows 需要另接计划任务；
  界面上的「一键生成」在两个平台都正常。
- macOS 包只有 **arm64**（Apple Silicon），没有 Intel 版本。
- 渲染需要一个 Chromium 内核的浏览器。Windows 10/11 自带 Edge，macOS 需要自行安装
  Chrome / Edge / Brave 之一。
- Windows 包在本机无法测试，如有问题请提 Issue。
