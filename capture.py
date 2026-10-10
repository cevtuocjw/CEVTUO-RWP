# -*- coding: utf-8 -*-
"""用**你真实 Chrome 的 profile 副本**做浏览器渲染取全页 HTML。

为什么必须这样做(而不是像老的 browser.py 那样用独立 profile + 注入 Cookie):
  * 你的 Chrome 里装了 Bypass Paywalls Clean 之类的扩展,正文是**扩展在浏览器里**
    解出来的,导几个 Cookie 出来根本复制不了这个效果;
  * archive.today 这类站点要真实浏览器把 JS 跑完才有正文;
  * Chrome 136 起,官方**禁止对默认用户目录开启 --remote-debugging-port**,
    所以 `open -a "Google Chrome" --args --remote-debugging-port=9222` 已经不管用了。

做法:把真实 profile(含扩展、登录态)rsync 一份到 state/chrome_user,
用它启动一个带调试口的 Chrome,通过 CDP 打开页面、等渲染稳定、读回 outerHTML。
"""
import json
import re
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import requests
import websocket

log = logging.getLogger("capture")

import paths

BASE = paths.ensure()          # 数据目录(可写);源码在 paths.CODE_DIR
COPY_PROFILE = BASE / "state" / "chrome_user"
DEFAULT_PORT = 9222

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

# 支持的 Chromium 系浏览器。程序会去连**用户自己装的浏览器**(连同他装好的扩展),
# 所以这里要覆盖常见几种,而不是只认 Chrome。按顺序探测,第一个装了且用过 profile 的胜出;
# 若系统默认浏览器就在列表里,优先用它。
# 元组:(显示名, 默认浏览器标识, 可执行文件, profile 相对位置)
_CHROMIUM_MAC = [
    ("Google Chrome", "com.google.chrome",
     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
     "Library/Application Support/Google/Chrome"),
    ("Microsoft Edge", "com.microsoft.edgemac",
     "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
     "Library/Application Support/Microsoft Edge"),
    ("Brave", "com.brave.Browser",
     "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
     "Library/Application Support/BraveSoftware/Brave-Browser"),
    ("Arc", "company.thebrowser.Browser",
     "/Applications/Arc.app/Contents/MacOS/Arc",
     "Library/Application Support/Arc/User Data"),
    ("Vivaldi", "com.vivaldi.Vivaldi",
     "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
     "Library/Application Support/Vivaldi"),
    ("Chromium", "org.chromium.Chromium",
     "/Applications/Chromium.app/Contents/MacOS/Chromium",
     "Library/Application Support/Chromium"),
    ("Opera", "com.operasoftware.Opera",
     "/Applications/Opera.app/Contents/MacOS/Opera",
     "Library/Application Support/com.operasoftware.Opera"),
]

# Windows:可执行文件在 Program Files,profile 在 %LOCALAPPDATA%
_CHROMIUM_WIN = [
    ("Google Chrome", "ChromeHTML",
     r"C:\Program Files\Google\Chrome\Application\chrome.exe",
     r"Google\Chrome\User Data"),
    ("Google Chrome", "ChromeHTML",
     r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
     r"Google\Chrome\User Data"),
    ("Microsoft Edge", "MSEdgeHTM",
     r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
     r"Microsoft\Edge\User Data"),
    ("Microsoft Edge", "MSEdgeHTM",
     r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
     r"Microsoft\Edge\User Data"),
    ("Brave", "BraveHTML",
     r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
     r"BraveSoftware\Brave-Browser\User Data"),
    ("Vivaldi", "VivaldiHTM",
     r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
     r"Vivaldi\User Data"),
    ("Chromium", "ChromiumHTM",
     r"C:\Program Files\Chromium\Application\chrome.exe",
     r"Chromium\User Data"),
    ("Opera", "OperaStable",
     r"C:\Program Files\Opera\launcher.exe",
     r"Opera Software\Opera Stable"),
]

_CHROMIUM_RAW = _CHROMIUM_WIN if IS_WIN else _CHROMIUM_MAC


def _profile_root(rel):
    """把 profile 的相对描述解析成绝对路径。"""
    if IS_WIN:
        import os as _os
        base = _os.environ.get("LOCALAPPDATA") or (
            Path.home() / "AppData" / "Local")
        return Path(base) / rel
    return Path.home() / rel


# (显示名, 默认浏览器标识, 可执行文件, profile 绝对路径)
_CHROMIUM = [(n, b, e, _profile_root(p)) for (n, b, e, p) in _CHROMIUM_RAW]

# 这几个由 detect_browser() 填充(模块级,便于其它函数直接用)
CHROME = _CHROMIUM[0][2]
REAL_PROFILE = _CHROMIUM[0][3]
BROWSER_NAME = _CHROMIUM[0][0]
BROWSER_EXE = CHROME


def _default_browser_id():
    """问系统:https 的默认打开方式是谁。"""
    if IS_MAC:
        try:
            out = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure"],
                capture_output=True, text=True).stdout
            import re as _re
            for m in _re.finditer(r'LSHandlerURLScheme\s*=\s*"?https"?;.*?'
                                  r'LSHandlerRoleAll\s*=\s*"([^"]+)"', out, _re.S):
                return m.group(1)
        except Exception:
            pass
        return ""
    # Windows:从注册表读 https 的 ProgId
    try:
        import winreg  # noqa: F401  (仅 Windows 存在)
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations"
            r"\UrlAssociations\https\UserChoice")
        return winreg.QueryValueEx(k, "ProgId")[0]
    except Exception:
        return ""


def detect_browser():
    """挑一个可用的 Chromium 系浏览器,返回 {name, exe, profile} 并更新模块全局。"""
    global CHROME, REAL_PROFILE, BROWSER_NAME, BROWSER_EXE
    default_id = _default_browser_id()

    def use(entry, is_default):
        global CHROME, REAL_PROFILE, BROWSER_NAME, BROWSER_EXE
        name, _bid, exe, prof = entry
        CHROME, REAL_PROFILE, BROWSER_NAME, BROWSER_EXE = exe, prof, name, exe
        return {"name": name, "exe": exe, "profile": str(prof),
                "is_default": is_default}

    # 1) 默认浏览器(能对上标识的)优先
    for entry in _CHROMIUM:
        if entry[1] == default_id and Path(entry[2]).exists() \
                and profile_ok(entry[3]):
            return use(entry, True)
    # 2) 否则第一个装了且用过 profile 的
    for entry in _CHROMIUM:
        if Path(entry[2]).exists() and profile_ok(entry[3]):
            return use(entry, entry[1] == default_id)
    # 3) 都没有:退回列表第一项,后面给明确报错
    use(_CHROMIUM[0], False)
    return {"name": BROWSER_NAME, "exe": CHROME, "profile": str(REAL_PROFILE),
            "is_default": False, "missing": True}


def profile_ok(p):
    """profile 目录里得有 Default/Preferences,才算"这个浏览器被用过"。"""
    return (p / "Default" / "Preferences").exists()


# 导入时先探一次,让 sync/status 一开始就有正确的浏览器
detect_browser()

# 这些是纯缓存/大块无用数据,同步时排除,避免几 GB 的复制
EXCLUDES = [
    # 缓存
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
    "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "GraphiteDawnCache",
    "Crashpad", "component_crx_cache", "extensions_crx_cache",
    "Service Worker/CacheStorage", "Service Worker/ScriptCache",
    "optimization_guide_hint_cache_store", "optimization_guide_model_store",
    "Safe Browsing", "BrowserMetrics", "segmentation_platform",
    # 又大又对本用途没用(实测 6.1G 的 profile 主要靠这些省下来)
    "History", "History-journal", "Favicons", "Favicons-journal",
    "Shared Dictionary", "File System", "blob_storage", "WebStorage",
    "Session Storage", "Site Characteristics Database", "Sync Data",
    "Platform Notifications", "EncryptedBookmarks2",
    "EncryptedBookmarks2.bak", "Top Sites", "Visited Links",
    "Network Action Predictor", "Shortcuts", "Media History",
    "DIPS", "Trust Tokens", "Reporting and NEL", "AutofillStrikeDatabase",
]
# 必须保留(登录态/扩展就靠它们):Extensions、Local Extension Settings、
# Local Storage、IndexedDB、Cookies、Login Data、Preferences、Secure Preferences

_lk = threading.Lock()
_sync_state = {"copied": False, "at": 0, "msg": ""}


def _log(m):
    log.info(m)


# ------------------------------------------------------------------ 状态 -----
def port_alive(port=DEFAULT_PORT, timeout=1.5):
    try:
        r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def browser_version(port=DEFAULT_PORT):
    try:
        return requests.get(f"http://127.0.0.1:{port}/json/version",
                            timeout=2).json()
    except Exception:
        return None


def profile_copied():
    return (COPY_PROFILE / "Default" / "Preferences").exists()


def status(port=DEFAULT_PORT):
    return {
        "port": port,
        "browser": BROWSER_NAME,
        "browser_exe": CHROME,
        "chrome_running": port_alive(port),
        "profile_copied": profile_copied(),
        "profile_dir": str(COPY_PROFILE),
        "real_profile": str(REAL_PROFILE),
        "version": (browser_version(port) or {}).get("Browser"),
        "unpacked": unpacked_extension_paths(),
        "sync": _sync_state,
        "chrome_exists": Path(CHROME).exists(),
        "profile_exists": profile_ok(REAL_PROFILE),
    }


# ------------------------------------------------------------- 同步 profile ---
def sync_profile(force=False, wait=True):
    """把用户自己浏览器的 profile 同步到副本(连同他装好的扩展与登录态)。

    整目录 rsync(靠 EXCLUDES 瘦身),这样多 profile(Default / Profile 1 …)
    和扩展目录都能带过来。浏览器正在用副本时不能同步。
    """
    detect_browser()                 # 每次同步前重新探测一次,换浏览器也能跟上
    if not profile_ok(REAL_PROFILE):
        _sync_state.update(
            copy_ok=False,
            msg=f"找不到可用的浏览器 profile: {REAL_PROFILE}")
        return False
    if profile_copied() and not force:
        return True
    if not wait:
        threading.Thread(target=sync_profile, kwargs={"force": force},
                         daemon=True).start()
        return True
    with _lk:
        COPY_PROFILE.mkdir(parents=True, exist_ok=True)
        _log(f"同步 {BROWSER_NAME} profile → {COPY_PROFILE}(首次约 10–60 秒)")
        args = ["rsync", "-a", "--delete"]
        for e in EXCLUDES:
            args += ["--exclude", e]
        subprocess.run(args + [str(REAL_PROFILE) + "/", str(COPY_PROFILE) + "/"],
                       capture_output=True)
        exts = unpacked_extension_paths()
        if exts:
            _log(f"带过来 {len(exts)} 个开发者模式扩展:" +
                 ", ".join(Path(p).name for p in exts))
        _sync_state.update(copied=True, copy_ok=True, browser=BROWSER_NAME,
                           at=time.time(), msg="已同步")
        _log("profile 同步完成")
    return True


def refresh_profile():
    """强制重新同步(会先关掉自动化 Chrome)。"""
    kill_chrome()
    time.sleep(1.5)
    return sync_profile(force=True)


# ------------------------------------------------------------- 启动 Chrome ---
def unpacked_extension_paths():
    """从副本的 Secure Preferences 里找出所有「开发者模式加载」的扩展路径。

    为什么必须这么做:未打包扩展(MV3、如 Bypass Paywalls Clean)的权限授予记录
    存在 Secure Preferences 的 MAC 保护块里,复制到新 profile 后往往不再被认账
    —— 结果就是扩展加载了但 host 权限为空,declarativeNetRequest 规则一条都不生效,
    付费墙页面依然拿不到全文。用 --load-extension 显式加载时,Chrome 会按 manifest
    声明的 host_permissions 直接授权,绕开这个问题。
    """
    import json as _json
    out = []
    for name in ("Secure Preferences", "Preferences"):
        p = COPY_PROFILE / "Default" / name
        if not p.exists():
            continue
        try:
            d = _json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for eid, s in (d.get("extensions", {}).get("settings", {}) or {}).items():
            if s.get("location") != 4:          # 4 = LOAD_UNPACKED
                continue
            path = s.get("path")
            if path and Path(path).exists() and path not in out:
                out.append(path)
    if out:
        return out

    # ⚠️⚠️ 兜底:profile 里查不到就**去磁盘上找**。
    #
    # 这一段是必须的,因为这里是**先有鸡还是先有蛋**:
    # 扩展路径本来就只记在 profile 的 Preferences 里,而那个文件会被
    # sync_profile() 的 rsync 冲掉(真实 Chrome 的 Preferences 里当然没有
    # --load-extension 的记录)。记录一没,这里就永远返回空 ⇒ 扩展永远不加载
    # ⇒ **付费墙静默失效** —— 页面照常打开,只是拿到的永远是预览版,
    # 而且全程没有任何报错。
    #
    # 找不到的时候要**喊出来**。用户看到「没找到扩展」比拿到半篇正文强。
    for d in _discover_extensions():
        out.append(str(_adopt_extension(d)))
    if out:
        _log(f"profile 里没有未打包扩展记录,改从磁盘找到 {len(out)} 个: "
             + ", ".join(Path(p).name for p in out))
    else:
        _log("⚠️ 一个未打包扩展都没找到 —— 遇到付费墙的页面会只拿到预览版正文")
    return out


# 兜底搜索的位置。➊ 是我们自己的副本(找到一次之后就都从这拿,最稳)
_EXT_SEARCH = (
    BASE / "extensions",
    Path.home() / "Downloads",
    Path.home() / "Documents",
    Path.home() / "Desktop",
)
# 名字里带这些的优先(用户装的就是这类)。只是**排序**偏好,不是硬判据。
_EXT_HINT = re.compile(r"paywall|bypass|clean|unlock", re.I)


def _looks_like_ext(d):
    """目录是不是一个 Chrome 扩展。

    判据是 manifest.json 里**既有 manifest_version 又有实际声明**
    —— 只判"有没有 manifest.json"会把一堆无关目录(比如某些前端项目)也捞进来。
    """
    import json as _json
    try:
        m = _json.loads((d / "manifest.json").read_text(encoding="utf-8",
                                                       errors="ignore"))
    except Exception:
        return False
    if "manifest_version" not in m:
        return False
    return bool(m.get("declarative_net_request") or m.get("content_scripts")
                or m.get("host_permissions") or m.get("permissions"))


def _discover_extensions():
    import json as _json
    found = []
    for root in _EXT_SEARCH:
        if not root.is_dir():
            continue
        cands = []
        if (root / "manifest.json").exists():
            cands.append(root)
        try:
            cands += [d for d in root.iterdir()
                      if d.is_dir() and (d / "manifest.json").exists()]
        except Exception:
            continue
        for d in cands:
            if _looks_like_ext(d) and d not in found:
                found.append(d)
    found.sort(key=lambda d: 0 if _EXT_HINT.search(d.name) else 1)
    return found


def _adopt_extension(src):
    """把这个扩展**复制进数据目录**,以后不再依赖它原来在哪。

    ⚠️ 用户可能某天把 Downloads 里的那个文件夹删了/挪了/重命名了,
    那时扩展又会失效。收进我们自己的目录就一劳永逸。
    """
    import shutil
    dst_root = BASE / "extensions"
    dst = dst_root / src.name
    try:
        dst_root.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        _log(f"已把扩展「{src.name}」收进 {dst_root}(以后不依赖原位置)")
        return dst
    except Exception as e:
        _log(f"复制扩展失败,继续用原位置 {src}: {e}")
        return src


# 一个正常的桌面 Chrome UA。⚠️ headless 默认的 UA 里带 `HeadlessChrome`,
# 站点认出来就会发付费墙 —— 实测 FT 从 5918 字掉到 145 字。所以必须显式覆盖。
UA_DESKTOP = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")


# 机器人校验 / 反爬中间页。⚠️⚠️ 这一条是 2026-10-10 那个「FT 只抓到 145 字」的根因。
#
# 这类页面最坑的地方是：**它看起来跟一个加载好的正常页面一模一样**——
#   · document.readyState 是 complete（它确实加载完了）
#   · 正文只有几百字，而且**十几秒一个字都不变**（在等后端校验）
# ⇒ goto() 里「稳定 settle 秒就收」那条判断**必然会把它收下来**，
#   因为「稳定」正是它的特征。收到的是一张写着「请稍候…」的纸。
#
# 实测（2026-10-10，FT）：0~11 秒 innerText 恒为 554 字、标题「请稍候…」，
# 第 12 秒才换成真文章 5695 字。而原来 1.2 秒就返回了。
#
# ⇒ 判据不能是「它稳不稳定」，得是「**它像不像拦截页**」。像就继续等。
_INTERSTITIAL = re.compile(
    r"(security verification|just a moment|checking your browser|"
    r"verify (?:you are|that you are) human|enable javascript and cookies to continue|"
    r"ddos protection|cf-browser-verification|cf_chl_|"
    r"请稍候|正在检查您的浏览器|正在验证|验证成功)", re.I)


class Cancelled(Exception):
    """用户点了「取消」。

    ⚠️⚠️ 它**必须一路抛到最外层** —— 中途任何一层 `except Exception` 把它
    当成"这个站抓失败了"咽掉,后果都很严重:fetch_html 会**转身去试直连**,
    而直连没有取消机制,能再挂 25 秒。
    实测(2026-10-10):一个永远加载不完的页面,Chrome 那边 2.5 秒就取消了,
    结果整批**又等了 27.6 秒** —— 用户看到的就是「点了取消没反应」。
    """


def _looks_interstitial(text):
    """这段可见文字像不像「拦截中间页」？

    ⚠️ 两个条件都要满足：长得很像 + **确实很短**。
    只看关键词会误伤——讲 Cloudflare 的文章正文明明写着 "Just a moment"。
    """
    if not text:
        return False
    t = text.strip()
    return len(t) < 3000 and bool(_INTERSTITIAL.search(t[:1500]))


def _clear_saved_window():
    """删掉 Chrome 上次存下来的窗口位置。

    ⚠️⚠️ 不删的话 `--window-position` **完全不生效** —— Chrome 会先恢复上次的
    窗口位置(实测存的是 left=0, top=34, right=1280, bottom=1040,正好在屏幕正中),
    命令行给的坐标被它盖掉。症状就是「明明写了挪到屏幕外,窗口还是弹在正中间」。

    只能在 Chrome **没在跑**的时候改,否则会被它回写覆盖 —— launch() 本来就只在
    端口不通时调用,所以这里是安全的。
    """
    import json as _json
    for name in ("Default", "Profile 1", "Profile 2", "Profile 3"):
        p = COPY_PROFILE / name / "Preferences"
        if not p.exists():
            continue
        try:
            d = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        b = d.get("browser")
        if not isinstance(b, dict):
            continue
        keys = ("window_placement", "window_placement_popup",
                "app_window_placement")
        if not any(k in b for k in keys):
            continue
        for k in keys:
            b.pop(k, None)
        try:
            p.write_text(_json.dumps(d), encoding="utf-8")
            _log(f"已清掉 {name} 保存的窗口位置"
                 f"(不清的话 --window-position 不生效)")
        except Exception as e:
            _log(f"清窗口位置失败 {p}: {e}")


def launch(port=DEFAULT_PORT, headless=True):
    """起自动化 Chrome。

    ⚠️⚠️ 默认 **headless=new + 一个正常的 UA**,这两件事**必须成对**:

        headless=new                 → FT 只抽到  145 字(付费墙**没**被绕过)
        headless=new + 正常 UA       → FT 抽到 5918 字(和有窗口时**一模一样**)
        有窗口(旧做法)               → FT 抽到 5918 字

    原因是 headless 的默认 UA 里带 `HeadlessChrome`,站点一眼认出来就发付费墙。
    把 UA 换成正常的就分辨不出了。

    为什么要换:旧做法开着窗口,用户导入时会看见一个 Chrome 弹到最前面一页页翻
    (原话:「有时候总是跳出来弹窗」)。试过 `--window-position=-2400,-2400`,
    **没用** —— macOS 会把窗口位置夹回可见区域(实测窗口还是在 0,34 1280×1006)。
    headless 才是干净的解法。真遇到哪个站不认,把这个函数的 headless 传 False 即可。
    """
    if not Path(CHROME).exists():
        return False, "找不到 Google Chrome"
    if not sync_profile():
        return False, "profile 同步失败"
    # ⚠️ 这里以前会调 _clear_saved_window()(重写 Default/Preferences 清掉
    #    保存的窗口位置)。**已经删掉了** ——
    #    ① 现在跑 headless,压根没有窗口要摆位置,那一步没有意义;
    #    ② 重写 Preferences 会把扩展的登记记录一起弄丢,而扩展路径**正是从那读的**
    #       ⇒ 扩展再也加载不上 ⇒ 付费墙静默失效(实测 FT 从 5918 字掉到 145 字)。
    #    教训:为了修一个视觉小问题去重写 Chrome 的 profile 文件,把主功能弄坏了。
    COPY_PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        CHROME,
        f"--user-data-dir={COPY_PROFILE}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=ChromeWhatsNewUI,PrivacySandboxSettings4",
        "--window-size=1280,1000",
    ]
    # ⚠️⚠️ `--headless=new` 必须排在 `--user-agent` **前面**。
    #    实测同一条参数、只换这两者的先后,FT 就是 145 字 与 5918 字 的区别 ——
    #    两个参数都会碰 UA,谁在后谁说了算。这种坑光看代码永远看不出来,
    #    只有把两条命令行打印出来逐行对比才会发现"唯一的差异是顺序"。
    if headless:
        args.append("--headless=new")
    # ⚠️ UA 也不能省:headless 默认 UA 里带 HeadlessChrome,站点认出来就发付费墙。
    args.append(f"--user-agent={UA_DESKTOP}")
    unpacked = unpacked_extension_paths()
    if unpacked:
        # ⚠️ 每个扩展**单独一个 --load-extension**,不要拼成 "a,b"。
        #    实测:拼逗号那种写法扩展会被加载但**不生效**(FT 只拿到 145 字付费墙预览),
        #    而重复旗标就正常(5918 字)。两种写法在文档里都"合法",
        #    差别只在行为上 —— 光看代码看不出来。
        for _p in unpacked:
            args.append("--load-extension=" + str(_p))
        _log(f"显式加载未打包扩展 {len(unpacked)} 个: "
             + ", ".join(Path(p).name for p in unpacked))
    args.append("about:blank")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # ⚠️ 给 90 秒。原来只等 20 秒 —— 首次同步完 profile 之后 Chrome 要建索引、
    #    迁移数据,经常超过 20 秒,于是**第一次导入永远失败**,用户再点一次才行。
    #    (它看起来像"网络问题",其实是浏览器还没起来。)
    for _ in range(180):
        if port_alive(port):
            _log(f"自动化 Chrome 就绪(端口 {port})")
            return True, ""
        time.sleep(0.5)
    return False, "Chrome 启动超时(90 秒内调试端口没就绪)"


def kill_chrome():
    """关掉自动化浏览器。两个平台的进程管理命令完全不同:
    macOS 用 pkill,Windows 没有 pkill,得走 PowerShell 按命令行匹配。

    关掉之后必须让 _ext_done 复位 —— 下次起浏览器要重新走一遍
    Extensions.loadUnpacked,否则扩展会"加载了但没权限"。
    """
    global _ext_done
    _ext_done = False
    if IS_WIN:
        ps = ("Get-CimInstance Win32_Process | "
              "Where-Object { $_.CommandLine -like "
              f"'*user-data-dir={COPY_PROFILE}*' }} | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
              "-ErrorAction SilentlyContinue }")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=30)
        except Exception as e:
            _log(f"关闭浏览器失败: {e}")
    else:
        # ⚠️ 一遍杀不干净。Chrome 有 20+ 个 helper 进程,而且 pkill 发出后
        #    它们不会立刻全死 —— 实测跑完 pkill 还剩 2 个。
        for sig in ("-TERM", "-KILL"):
            subprocess.run(["pkill", sig, "-f", f"user-data-dir={COPY_PROFILE}"],
                           capture_output=True)
            time.sleep(1.0)
            r = subprocess.run(["pgrep", "-f", f"user-data-dir={COPY_PROFILE}"],
                               capture_output=True, text=True)
            if not (r.stdout or "").strip():
                break
        # 等端口真的空出来
        for _ in range(24):
            if not port_alive():
                break
            time.sleep(0.5)

    # ⚠️⚠️ 清掉 Chrome 的 singleton 锁 —— 这一步不能省,而且害处极隐蔽。
    #
    # Chrome 的规矩:同一个 user-data-dir **已经有实例在跑**时,新启动的进程会
    # "加入"那个旧实例、自己退出。于是你新给的 `--headless=new`、`--user-agent`、
    # `--load-extension` **全部白写** —— 你连上的是那个**旧浏览器**。
    #
    # 症状就是这次查了半天的东西:配置一模一样,行为却时好时坏
    # (FT 一会儿 5918 字一会儿 145 字),因为"连上的是哪一个浏览器"
    # 取决于上一次有没有杀干净 —— 而这件事在代码里完全看不出来。
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (COPY_PROFILE / name).unlink()
        except Exception:
            pass
    time.sleep(0.5)


def load_unpacked_via_cdp(port=DEFAULT_PORT):
    """用 CDP 的 Extensions.loadUnpacked 真正加载未打包扩展。

    ⚠️⚠️ 为什么不能只靠 --load-extension(这是踩了很久才定位到的坑):
        Chrome 153 下,--load-extension 能让扩展"出现在扩展列表里",
        但**不给 host 权限** —— extensions-internals 里 explicit_host 始终是 0,
        于是 declarativeNetRequest 规则一条都不生效,付费墙照样挡着。
        必须走 CDP 的 Extensions.loadUnpacked,才会按 manifest 声明的
        host_permissions 真正授权。

    实测(同一篇 FT 文章):
        只有 --load-extension      → DOM 里 9 个 <p>,抽出 1053 字(付费墙预览)
        加上 loadUnpacked          → DOM 里 23 个 <p>,抽出 3625 字(完整正文)
    """
    paths = unpacked_extension_paths()
    if not paths:
        return []
    try:
        v = requests.get(f"http://127.0.0.1:{port}/json/version",
                         timeout=8).json()
        ws = websocket.create_connection(v["webSocketDebuggerUrl"], timeout=30)
    except Exception as e:
        _log(f"连不上浏览器调试口: {e}")
        return []
    loaded, mid = [], 0
    for p in paths:
        mid += 1
        try:
            ws.send(json.dumps({"id": mid,
                                "method": "Extensions.loadUnpacked",
                                "params": {"path": p}}))
            ws.settimeout(45)
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == mid:
                    rid = (m.get("result") or {}).get("id")
                    if rid:
                        loaded.append((Path(p).name, rid))
                    else:
                        _log(f"loadUnpacked 未返回 id: "
                             f"{json.dumps(m, ensure_ascii=False)[:160]}")
                    break
        except Exception as e:
            _log(f"loadUnpacked 失败 {p}: {e}")
    try:
        ws.close()
    except Exception:
        pass
    return loaded


_ext_done = False


def ensure(port=DEFAULT_PORT):
    """确保浏览器可用,并且**未打包扩展已经真正拿到权限**。"""
    global _ext_done
    if not port_alive(port):
        ok, err = launch(port)
        if not ok:
            return False, err
    if not _ext_done:
        loaded = load_unpacked_via_cdp(port)
        if loaded:
            _log("已通过 CDP 授权未打包扩展: "
                 + ", ".join(f"{n}" for n, _ in loaded))
            # ⚠️⚠️ 加载完**必须等一下**再抓第一页。
            #
            # 付费墙扩展的拦截规则是它的 service worker 在**运行时**装进去的,
            # 而 loadUnpacked 只是把扩展挂上 —— 两者之间有个空窗。
            # 第一页要是抢在这个空窗里加载,规则还没生效,拿到的就是付费墙预览。
            # 实测同一篇 FT(同一套配置,只是时序不同):
            #     不等 → 145 / 1050 字(付费墙预览)   等 3 秒 → 5918 字(全文)
            # 所以「第一次导入总是不全、再点一次就好了」就是这个空窗。
            time.sleep(3.0)
        _ext_done = True
    return True, ""


# ------------------------------------------------------------------- CDP -----
class Tab:
    """一个 CDP 标签页会话。"""

    def __init__(self, port, timeout=60):
        self.port = port
        self.timeout = timeout
        self.base = f"http://127.0.0.1:{port}"
        tgt = requests.put(self.base + "/json/new?about:blank", timeout=8).json()
        self.id = tgt.get("id")
        self._saw_challenge = False      # goto() 里认出了机器人校验中间页
        self.ws = websocket.create_connection(tgt["webSocketDebuggerUrl"],
                                              timeout=20)
        self._mid = 0
        self.cmd("Page.enable")
        self.cmd("Runtime.enable")
        self.cmd("Network.enable")

    def cmd(self, method, params=None, timeout=None):
        self._mid += 1
        mid = self._mid
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        self.ws.settimeout(timeout or self.timeout)
        while True:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                return None
            if msg.get("id") == mid:
                return msg

    def evaluate(self, expr, timeout=None):
        r = self.cmd("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True}, timeout)
        return (((r or {}).get("result") or {}).get("result") or {}).get("value")

    def set_cookies(self, cookies):
        ok = 0
        for c in cookies or []:
            if not c.get("name"):
                continue
            p = {"name": c["name"], "value": c.get("value", ""),
                 "domain": c.get("domain", ""), "path": c.get("path", "/"),
                 "secure": bool(c.get("secure"))}
            r = self.cmd("Network.setCookie", p)
            if (r or {}).get("result", {}).get("success"):
                ok += 1
        return ok

    def goto(self, url, wait_text=350, settle=1.2, max_wait=None,
             should_stop=None, challenge_wait=30.0):
        """打开页面并等正文稳定。返回渲染后的 outerHTML。

        `should_stop` 是个回调,每轮轮询都会问一次 —— 用户点了「取消」
        之后最多 0.5 秒就能退出,而不是把当前这个网址的 60 秒超时耗完。
        用户原话:「如果卡壳了我就没有任何办法」。

        `challenge_wait` 是**专给拦截中间页**的额外预算(见 _INTERSTITIAL)。
        主体超时 `max_wait` 照旧管普通页面;只有确实认出是拦截页时才额外等,
        免得正常页面莫名其妙慢一倍。
        """
        max_wait = max_wait or self.timeout
        self.cmd("Page.navigate", {"url": url})
        deadline = time.time() + max_wait
        last, stable_at = -1, time.time()
        chal_until = time.time() + challenge_wait   # 拦截页的总预算
        saw_chal = False
        chal_cleared = False
        while True:
            if time.time() >= deadline and not (
                    saw_chal and time.time() < chal_until):
                break
            if should_stop and should_stop():
                raise Cancelled("已取消")
            ready = False
            text = ""
            try:
                n = self.evaluate(
                    "document.body ? document.body.innerText.length : 0", 5) or 0
                ready = self.evaluate("document.readyState", 5) == "complete"
            except Exception:
                n = 0
            # ⚠️ 只有可疑的时候才多取一次 innerText —— 它是页面级的字符串,
            #    每 0.5 秒无脑拷一次会在长文上白烧时间。
            if n and n < 3000:
                try:
                    text = self.evaluate(
                        "document.body ? document.body.innerText : ''", 5) or ""
                except Exception:
                    text = ""
            if _looks_interstitial(text):
                # ⚠️⚠️ 关键的一行:**把它当成"页面还没好"**,而不是"稳定了"。
                # 不重置 stable_at 的话,它 1.2 秒后就会被当成成品收走 ——
                # 「稳定」恰恰是中间页的特征,拿"稳定"当判据必然中招。
                saw_chal = True
                last, stable_at = -1, time.time()
                time.sleep(0.5)
                continue
            if saw_chal and not chal_cleared and n > 0:
                # 拦截页刚放行 —— 真页面这才开始加载。
                # ⚠️ 必须**把预算补回来**,否则"等校验用掉的那 12 秒"会从
                #    真页面的加载时间里扣,真页面还没load完就被 deadline 掐断。
                chal_cleared = True
                deadline = max(deadline, time.time() + min(max_wait, 25))
            if n != last:
                # 正文还在增长(懒加载/JS 渲染中),继续等
                last, stable_at = n, time.time()
            elif n > 0 and time.time() - stable_at >= settle and (
                    ready or time.time() - stable_at >= 6.0):
                # 稳定 settle 秒 **且** 文档已加载完 —— 才收。
                # ⚠️ 只看"稳定"是不够的:页面可以是"加载中但正文没变",
                #    那时收下来会缺一大截。要求 readyState=complete 才敢收。
                #    后面那个 6 秒兜底是给"卡在广告/统计脚本上永远不 complete"的页面。
                break
            if n >= 20000:
                break
            time.sleep(0.5)
        if saw_chal:
            self._saw_challenge = True
        # 让懒加载图片站一站
        try:
            self.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
            time.sleep(0.3)
            self.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        return self.evaluate("document.documentElement.outerHTML")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            requests.get(f"{self.base}/json/close/{urllib.parse.quote(self.id)}",
                         timeout=3)
        except Exception:
            pass


# --------------------------------------------------------------- 对外接口 ----
def render(url, port=DEFAULT_PORT, timeout=60, wait_text=350, settle=1.2,
           cookies=None, should_stop=None):
    """用真实 Chrome 渲染 url,返回 (html, err)。"""
    ok, err = ensure(port)
    if not ok:
        return "", err
    tab = None
    try:
        tab = Tab(port, timeout=timeout)
        if cookies:
            tab.set_cookies(cookies)
        html = tab.goto(url, wait_text=wait_text, settle=settle,
                        max_wait=timeout, should_stop=should_stop) or ""
        return html, ("" if html else "页面未返回内容")
    except Exception as e:
        return "", f"浏览器渲染失败: {e}"
    finally:
        if tab:
            tab.close()


def render_many(urls, port=DEFAULT_PORT, timeout=60, on_progress=None,
                cookies_for=None):
    """串行渲染多个 URL(共用一个 Chrome,逐页开标签)。

    cookies_for(url) 可返回要注入的 cookie 列表。
    产出 (url, html, err)。
    """
    ok, err = ensure(port)
    if not ok:
        for u in urls:
            yield u, "", err
        return
    for i, u in enumerate(urls):
        tab = None
        try:
            tab = Tab(port, timeout=timeout)
            ck = cookies_for(u) if cookies_for else None
            if ck:
                tab.set_cookies(ck)
            html = tab.goto(u, max_wait=timeout) or ""
            yield u, html, ("" if html else "空页面")
        except Exception as e:
            yield u, "", str(e)
        finally:
            if tab:
                tab.close()
            if on_progress:
                on_progress(i + 1, len(urls), u)


# ------------------------------------------------- 从真实 Chrome 导出 Cookie ---
def mac_chrome_cookies(domain_filter=None):
    """从真实 Chrome 的 Cookies 库里解出明文 cookie(用于给副本补登录态)。

    需要 Chrome Safe Storage 钥匙串密钥;首次会弹一次授权框。
    Chrome 锁库,必须先复制出来再读。
    """
    import shutil as _sh
    import sqlite3
    import tempfile

    src = REAL_PROFILE / "Default" / "Cookies"
    if not src.exists():
        return []
    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    try:
        _sh.copy2(src, tmp)
    except Exception as e:
        log.warning("复制 Cookies 失败: %s", e)
        return []
    try:
        key = subprocess.run(
            ["security", "find-generic-password", "-w", "-s",
             "Chrome Safe Storage", "-a", "Chrome"],
            capture_output=True, text=True).stdout.strip()
    except Exception:
        key = ""
    if not key:
        return []
    try:
        import hashlib
        dk = hashlib.pbkdf2_hmac("sha1", key.encode(), b"saltysalt", 1003, 16)
        iv = b" " * 16
        out = []
        con = sqlite3.connect(str(tmp))
        con.row_factory = sqlite3.Row
        sql = ("SELECT host_key,name,value,encrypted_value,path,is_secure,"
               "expires_utc FROM cookies")
        if domain_filter:
            sql += " WHERE host_key LIKE ?"
            con.execute(sql, (f"%{domain_filter}%",))
            cur = con.execute(sql, (f"%{domain_filter}%",))
        else:
            cur = con.execute(sql)
        for r in cur.fetchall():
            raw = r["encrypted_value"]
            val = r["value"]
            if raw:
                try:
                    pt = _aes_cbc_decrypt(dk, iv, raw)
                    if pt[:3] in (b"v10", b"v11"):
                        pt = pt[3:]
                    # Chrome 130+ 在明文前加了 32 字节域哈希前缀
                    txt = pt.decode("utf-8", "ignore")
                    if txt and not txt.isprintable():
                        txt = pt[32:].decode("utf-8", "ignore")
                    val = txt
                except Exception:
                    continue
            if not val:
                continue
            out.append({"name": r["name"], "value": val,
                        "domain": r["host_key"], "path": r["path"] or "/",
                        "secure": bool(r["is_secure"])})
        return out
    except Exception as e:
        log.warning("读取 Chrome cookie 失败: %s", e)
        return []
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def _aes_cbc_decrypt(key, iv, data):
    """AES-128-CBC 解密(优先 cryptography,退回 pycryptodome)。"""
    try:
        from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                            modes)
        c = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return c.update(data) + c.finalize()
    except Exception:
        from Crypto.Cipher import AES
        return AES.new(key, AES.MODE_CBC, iv).decrypt(data)


def open_in_real_chrome(url):
    """在你的日常 Chrome 里打开一个页面(不接管,只是打开)。"""
    subprocess.Popen(["open", "-a", "Google Chrome", url])
