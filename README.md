# rss-daily-epub

把 RSS 订阅每天的更新自动打包成 **Kindle 连续流** epub,按 OPML 分类各自成书。
「书」不再是点目录→进文章,而是把当天文章**全部顺序铺开**,像一本杂志一样直接一直往下翻。

产物示例(`output/2026-09-05/`):
```
ART＆FASHION-2026-09-05.epub   FINANCE-2026-09-05.epub   Japan-2026-09-05.epub  …
```
另有每源的统计与单页 Web 管理界面。

## 阅读体验(连续流 + 顶部窄条)

- 每个分类一本。打开即读:封面 →「今日源目录」→ 文章按 源→时间 连续排列,直接翻页不停顿。
- 每篇文章顶部有一行**极窄的导航条**:
  `‹上篇 | ☰目录 | 下篇›  ·  ‹上一源:xxx | 下一源:xxx›  ·  源列表 ↺`
- 「☰ 目录 / 源列表 ↺」回到最前面的「今日源目录」页,那里列出了每个源并可一键跳过去;
  设备自带的 TOC(分类)也可用,但阅读并不依赖它。

## 全文获取(合法途径,分四层 + 外接 + 兜底)

正文逐层尝试,每篇在统计与界面上标注它来自哪一层:

| 层 | 方式 | 标注 |
|---|---|---|
| L0 | RSS 自带全文(Substack 等) | `feed_full` |
| L1 | 文章公开页正文(trafilatura) | `public` |
| L2 | 站点公开的 AMP/打印页 | `amp_print` |
| L3 | **你自己的订阅 Cookie** 会话 | `cookie` |
| 外接 | `fulltext.ext.cmd` 指向你自己的工具 | `ext` |
| 兜底 | 仅 RSS 摘要 / 取不到 | `partial` / `blocked` |

> ⚠️ 说明:本项目不做“破解”式绕过付费墙(不会伪装爬虫/注入破解脚本)。
> 对你**确实订阅了**的站点(FT、Bloomberg、Economist…),最可靠的做法是给该域名提供
> 你的 Cookie:把浏览器里该站的登录 Cookie 导成 Netscape 格式
> (`cookies.txt`,或用 EditThisCookie 等导出),在 `config.json` 里按域名指向它,
> 管道就会用你的会话抓全文。仍取不到的篇目会被标成“仅摘要/未取得”,在界面里如实统计,
> 正文中也会标注“(此篇仅摘要)”。

### 用"日常 Chrome 登录态"渲染正文(对付 NYT/Bloomberg/Le Monde/Economist 的反爬)

这几个站对纯 HTTP 直接 403/402,连 Cookie 都过不去,必须真实浏览器渲染。
做法(纯你的登录会话,不涉及任何破解扩展):

1. 双击 **`启用Chrome调试端口.command`** —— 它会重启你的 Chrome 并开启本地调试口 9222;
2. 保持这个 Chrome 开着(你正常上网即可);重启电脑后记得再双击一次;
3. `config.json` 里已开启 `"browser":{"enabled":true,"port":9222}`;
4. 在管理界面点「生成/刷新今天」,引擎会对配置了 Cookie 的域名自动用你的 Chrome 渲染取正文(标注 `browser/Chrome`)。

> Chrome 没开调试口时该层会自动跳过、不报错,退回公开抓取/摘要。

可自行添加或者更换修改自己的rss实例。
已打包我的日常关注rss进入扩展作为默认例子

### config.json 结构示例

```jsonc
{
  "smtp": { "host": "smtp.qq.com", "port": 465,
            "user": "你的邮箱", "password": "授权码" },
  "mail": { "to": "xxx@kindle.com" },
  "cookies": {                       // 你有权限的站点 Cookie(Netscape 格式)
    "ft.com":       "/Users/你/Downloads/ft_cookies.txt",
    "economist.com":"/Users/你/Downloads/eco_cookies.txt"
  },
  "fulltext": {                       // 可选外接命令(把 URL 追加在命令后,stdout 返回正文)
    "ext": { "enabled": false, "cmd": ["/path/to/你的/脚本"] }
  }
}
```
`cookies.txt` 示例(Netscape):
```
# Netscape HTTP Cookie File
.ft.com	TRUE	/	FALSE	1728xxxx	sid	abcdef123456
```
把 `config.json.example` 复制为 `config.json` 后修改即可。Cookie 会按域名后缀自动匹配。

## 一次性准备

```bash
cd "/Users/cjw/Library/Application Support/RSSDailyEpub"
uv sync
```

## 手动运行

```bash
./run_daily.sh
# 或细分:
./.venv/bin/python rss2epub.py --category FINANCE
./.venv/bin/python rss2epub.py --no-fulltext     # 全站只用摘要(极快)
```

## 每天定时(本机 launchd,每天 08:00,已装好)

```bash
launchctl list | grep rssdaily          # 查看
launchctl unload ~/Library/LaunchAgents/com.rssdaily.epub.plist   # 停
# 重装: cp com.rssdaily.epub.plist ~/Library/LaunchAgents/ && launchctl load ...
```

## Web 管理界面

```bash
# 方式一:双击「启动管理器.command」
# 方式二:双击 RSSPublisher.app(会自开浏览器)
# 方式三:
./.venv/bin/python ui/app.py --open
# 打开 http://127.0.0.1:8611
```

界面提供:
- 日期切换(历史每一天的书)
- 一键「生成今天」(后台跑,进度条+日志实时刷新)
- 每分类/每源统计(用**折叠面板/手风琴**呈现):收录篇数、**全文几篇 / 仅摘要几篇 / 未取得几篇**,
  展开看每篇标注与原文链接
- 每本书「打开位置 / 打开阅读」

## 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--category` | 全部 | 只处理某些分类 |
| `--hours` | 24 | 收录窗口(小时) |
| `--max-per-feed` | 15 | 每源最多收录条数 |
| `--no-fulltext` | 关 | 不做全文抓取,只用摘要 |
| `--no-images` | 关 | 不下载内嵌图片 |
| `--max-images` | 600 | 全运行图片总数上限(每张已自动压到 ~1400px/约200KB 内) |
| `--send-email` | 关 | 生成合并本并发邮件 |
| `--workers`/`--timeout` | 8/25 | 抓取并发与超时 |

## 数据与日志

- `output/YYYY-MM-DD/` 生成的 epub
- `history/YYYY-MM-DD.json` 当日统计(每分类→每源→每篇模式)
- `state/seen.txt` 去重(自动清理 45 天前);`state/progress.json` 运行进度
- `logs/run.log` 定时运行、`logs/ui_run.log` 界面触发、`logs/rss2epub.log` 手动运行

## 注意

- 每天 08:00 跑,收录的是**过去 24 小时**更新(前晚内容不漏)。同一天重复跑会覆盖当天同名 epub。
- 图片过多导致 Kindle 邮件被拒时,减小 `--max-per-feed` 或加 `--no-images`。
- 个别源(rsshub 公共实例 403、停更源)抓取失败只记 warning,不影响其它源。
- 增删订阅请改 `feeds.opml`。
