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
    return out


def launch(port=DEFAULT_PORT, headless=False):
    if not Path(CHROME).exists():
        return False, "找不到 Google Chrome"
    if not sync_profile():
        return False, "profile 同步失败"
    COPY_PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        CHROME,
        f"--user-data-dir={COPY_PROFILE}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=ChromeWhatsNewUI,PrivacySandboxSettings4",
        "--window-size=1280,1800",
    ]
    unpacked = unpacked_extension_paths()
    if unpacked:
        args.append("--load-extension=" + ",".join(unpacked))
        _log(f"显式加载未打包扩展 {len(unpacked)} 个: "
             + ", ".join(Path(p).name for p in unpacked))
    if headless:
        args.append("--headless=new")
    args.append("about:blank")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if port_alive(port):
            _log(f"自动化 Chrome 就绪(端口 {port})")
            return True, ""
        time.sleep(0.5)
    return False, "Chrome 启动超时(调试端口未就绪)"


def kill_chrome():
    """关掉自动化浏览器。两个平台的进程管理命令完全不同:
    macOS 用 pkill,Windows 没有 pkill,得走 PowerShell 按命令行匹配。"""
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
        subprocess.run(["pkill", "-f", f"user-data-dir={COPY_PROFILE}"],
                       capture_output=True)
    time.sleep(1.0)


def ensure(port=DEFAULT_PORT):
    if port_alive(port):
        return True, ""
    return launch(port)


# ------------------------------------------------------------------- CDP -----
class Tab:
    """一个 CDP 标签页会话。"""

    def __init__(self, port, timeout=60):
        self.port = port
        self.timeout = timeout
        self.base = f"http://127.0.0.1:{port}"
        tgt = requests.put(self.base + "/json/new?about:blank", timeout=8).json()
        self.id = tgt.get("id")
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

    def goto(self, url, wait_text=350, settle=2.0, max_wait=None):
        """打开页面并等正文稳定。返回渲染后的 outerHTML。"""
        max_wait = max_wait or self.timeout
        self.cmd("Page.navigate", {"url": url})
        deadline = time.time() + max_wait
        last, stable_at = -1, time.time()
        while time.time() < deadline:
            try:
                n = self.evaluate(
                    "document.body ? document.body.innerText.length : 0", 5) or 0
            except Exception:
                n = 0
            if n != last:
                # 正文还在增长(懒加载/JS 渲染中),继续等
                last, stable_at = n, time.time()
            elif n > 0 and time.time() - stable_at >= settle:
                # 已经稳定 settle 秒 —— 不管长短都可以收了
                # (早先要求 n >= wait_text 才 break,导致小页面必定等满超时)
                break
            if n >= 20000:
                break
            time.sleep(0.5)
        # 让懒加载图片/脚本站稳
        try:
            self.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
            time.sleep(0.5)
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
def render(url, port=DEFAULT_PORT, timeout=60, wait_text=350, settle=2.0,
           cookies=None):
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
                        max_wait=timeout) or ""
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
