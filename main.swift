// CEVTUO-RWP —— 原生窗口外壳
//
// 早先的做法是 bash 启动器 + 在默认浏览器里开标签页,两个毛病:
//   1) 可执行文件是一个永不退出的服务进程,macOS 认为 GUI 一直没启动完 → Dock 一直弹跳
//   2) 用户拿到的是浏览器标签页,不是一个"应用"
// 现在改成真正的 AppKit 应用:自己开原生窗口(WKWebView),自己拉起本地 Python 服务,
// 退出时把服务一起收掉。

import Cocoa
import WebKit

let kPort = 8611
let kBase = URL(string: "http://127.0.0.1:8611")!
let kAppName = "CEVTUO-RWP"

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {

    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    var weStartedServer = false
    var loading = true

    // ---------------------------------------------------------------- 生命周期
    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        buildWindow()
        bringUpServer { [weak self] ok in
            guard let self = self else { return }
            if ok {
                var req = URLRequest(url: kBase)
                req.timeoutInterval = 15
                self.webView.load(req)
            } else {
                self.showFailure()
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ note: Notification) {
        // 只收掉自己拉起来的服务;如果本来就是别人在跑,别动它
        if weStartedServer { server?.terminate() }
    }

    // ------------------------------------------------------------------ 菜单
    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 \(kAppName)",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "在浏览器中打开",
                        action: #selector(openInBrowser), keyEquivalent: "b")
        appMenu.addItem(withTitle: "重新载入", action: #selector(reload), keyEquivalent: "r")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 \(kAppName)",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出 \(kAppName)",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        NSApp.mainMenu = main
    }

    @objc func openInBrowser() { NSWorkspace.shared.open(kBase) }
    @objc func reload() { webView.reload() }

    // ------------------------------------------------------------------ 窗口
    func buildWindow() {
        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: .zero, configuration: cfg)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsMagnification = true

        let r = NSRect(x: 0, y: 0, width: 1380, height: 900)
        window = NSWindow(contentRect: r,
                          styleMask: [.titled, .closable, .miniaturizable,
                                      .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = kAppName
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.minSize = NSSize(width: 960, height: 640)
        window.contentView = webView
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // ------------------------------------------------------- 本地服务:起/等
    func health(_ done: @escaping (Bool) -> Void) {
        var req = URLRequest(url: kBase.appendingPathComponent("api/state"))
        req.timeoutInterval = 1.0
        req.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { done(ok) }
        }.resume()
    }

    func bringUpServer(_ done: @escaping (Bool) -> Void) {
        health { [weak self] alive in
            guard let self = self else { return }
            if alive { done(true); return }          // 已经在跑,直接用
            self.spawnServer()
            self.waitReady(attempts: 80, done: done)
        }
    }

    func waitReady(attempts: Int, done: @escaping (Bool) -> Void) {
        if attempts <= 0 { done(false); return }
        health { [weak self] ok in
            if ok { done(true) }
            else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                self?.waitReady(attempts: attempts - 1, done: done) } }
        }
    }

    /// 找运行时:优先 app 自带的,退回开发目录
    func runtimeDir() -> URL? {
        let fm = FileManager.default
        if let res = Bundle.main.resourceURL {
            let bundled = res.appendingPathComponent("app")
            if fm.isExecutableFile(atPath: bundled.appendingPathComponent("python/bin/python3").path)
                && fm.fileExists(atPath: bundled.appendingPathComponent("ui/server.py").path) {
                return bundled
            }
            if fm.isExecutableFile(atPath: bundled.appendingPathComponent(".venv/bin/python").path)
                && fm.fileExists(atPath: bundled.appendingPathComponent("ui/server.py").path) {
                return bundled
            }
        }
        // 开发目录(未打包时)
        let dev = fm.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/RSSDailyEpub")
        if fm.fileExists(atPath: dev.appendingPathComponent("ui/server.py").path) { return dev }
        return nil
    }

    func dataDir() -> URL {
        let d = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/CEVTUO-RWP")
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    func spawnServer() {
        guard let dir = runtimeDir() else { return }
        let fm = FileManager.default
        let py: URL
        var env = ProcessInfo.processInfo.environment
        if fm.isExecutableFile(atPath: dir.appendingPathComponent("python/bin/python3").path) {
            py = dir.appendingPathComponent("python/bin/python3")
            env["PYTHONPATH"] = dir.appendingPathComponent("pylibs").path
        } else {
            py = dir.appendingPathComponent(".venv/bin/python")
        }
        env["CEVTUO_DATA_DIR"] = dataDir().path
        env["PYTHONUNBUFFERED"] = "1"

        let p = Process()
        p.executableURL = py
        p.arguments = [dir.appendingPathComponent("ui/server.py").path,
                       "--port", String(kPort)]
        p.environment = env
        p.currentDirectoryURL = dir
        let logURL = dataDir().appendingPathComponent("logs/server.log")
        try? FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !fm.fileExists(atPath: logURL.path) { fm.createFile(atPath: logURL.path, contents: nil) }
        if let fh = try? FileHandle(forWritingTo: logURL) {
            fh.seekToEndOfFile()
            p.standardOutput = fh
            p.standardError = fh
        }
        do { try p.run(); server = p; weStartedServer = true }
        catch { NSLog("spawn failed: \(error)") }
    }

    func showFailure() {
        let html = """
        <html><head><meta charset="utf-8"><style>
        body{font:15px/1.7 -apple-system,"PingFang SC",sans-serif;padding:60px;color:#333;
        background:#f6f7f9}div{max-width:560px;margin:0 auto;background:#fff;padding:28px 32px;
        border-radius:16px;box-shadow:0 10px 34px rgba(38,52,80,.13)}code{background:#eef1f5;
        padding:2px 6px;border-radius:6px}</style></head><body><div>
        <h2>本地服务启动失败</h2>
        <p>请查看日志:<br><code>~/Library/Application Support/CEVTUO-RWP/logs/server.log</code></p>
        <p>常见原因:程序自带的 Python 运行时缺失,或端口 \(kPort) 被占用。</p>
        </div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // ---------------------------------------------------------- 导航策略
    // 只有本地界面留在窗口里;其它链接(原文链接等)交给默认浏览器
    func webView(_ wv: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let u = action.request.url else { decisionHandler(.allow); return }
        if u.host == "127.0.0.1" || u.host == "localhost" || u.scheme == "file"
            || u.scheme == "about" || u.scheme == "data" {
            decisionHandler(.allow)
        } else if u.scheme == "http" || u.scheme == "https" {
            NSWorkspace.shared.open(u)
            decisionHandler(.cancel)
        } else {
            NSWorkspace.shared.open(u)
            decisionHandler(.cancel)
        }
    }

    // target=_blank / window.open → 外部浏览器
    func webView(_ wv: WKWebView, createWebViewWith cfg: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url { NSWorkspace.shared.open(u) }
        return nil
    }

    func webView(_ wv: WKWebView, didFinish navigation: WKNavigation!) { loading = false }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
