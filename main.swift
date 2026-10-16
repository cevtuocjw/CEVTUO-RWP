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
    var statusItem: NSStatusItem?

    // ---------------------------------------------------------------- 生命周期
    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        buildStatusItem()
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

        // ⚠️⚠️ 菜单栏上那些**系统快捷键是靠主菜单里真的存在那一项**才有的。
        //    以前这里只有「应用」和「编辑」两个菜单 —— 没有「窗口」菜单、
        //    也就没有「最小化」这一项 ⇒ **⌘M 按下去什么都不发生**,
        //    而且不报错、菜单里也找不到它(用户的原话:「能够 Command M 收回」)。
        //    同理,⌘W / ⌘Q / ⌘H 之所以能用,是因为上面那几项真的存在。
        //    ⇒ 要加什么快捷键,先加那一项,再谈绑定。
        let winItem = NSMenuItem()
        main.addItem(winItem)
        let win = NSMenu(title: "窗口")
        win.addItem(withTitle: "最小化",
                    action: #selector(NSWindow.performMiniaturize(_:)),
                    keyEquivalent: "m")
        win.addItem(withTitle: "缩放",
                    action: #selector(NSWindow.performZoom(_:)),
                    keyEquivalent: "")
        win.addItem(.separator())
        win.addItem(withTitle: "把窗口放到最前",
                    action: #selector(NSApplication.arrangeInFront(_:)),
                    keyEquivalent: "")
        winItem.submenu = win
        // ⚠️ 还要把它登记成 NSApp.windowsMenu —— 这样 AppKit 自己会往里面
        //    追加窗口列表、并把「最小化」和当前窗口接上(target 为 nil 时
        //    走响应链,只有登记了才是确定的行为)。
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }

    @objc func openInBrowser() { NSWorkspace.shared.open(kBase) }
    @objc func reload() { webView.reload() }

    // -------------------------------------------------------------- 菜单栏图标
    func buildStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem = item

        // 图标从 app 包里的 ui/ 取。⚠️ 两条路径都试:打包形态在 Resources/app,
        // 开发期直接跑源码时在开发目录 —— 和 runtimeDir() 同一套判断。
        var base: URL? = nil
        if let dir = runtimeDir() {
            let p = dir.appendingPathComponent("ui/statusbar.png")
            if FileManager.default.fileExists(atPath: p.path) { base = dir.appendingPathComponent("ui") }
        }
        var img: NSImage? = nil
        if let b = base {
            img = NSImage(contentsOfFile: b.appendingPathComponent("statusbar.png").path)
        }
        if let img = img {
            // ⚠️ isTemplate = 让系统按菜单栏主题(浅色/深色、选中态)自动反色。
            //    不设的话这块银色 logo 在深色菜单栏上会糊成一团看不见的东西。
            img.isTemplate = true
            img.size = NSSize(width: 18, height: 18)
            item.button?.image = img
            item.button?.toolTip = kAppName
        } else {
            // ⚠️ 图没找到时**不能什么都不显示** —— 那样菜单栏上就是一片空白,
            //    和「这个功能没做」长得一模一样。退回文字。
            item.button?.title = "RWP"
            item.button?.toolTip = kAppName + "(图标没找到,用的是文字)"
        }

        let menu = NSMenu()
        menu.addItem(withTitle: "显示 \(kAppName)",
                     action: #selector(showMainWindow), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "在浏览器中打开",
                     action: #selector(openInBrowser), keyEquivalent: "")
        menu.addItem(withTitle: "重新载入", action: #selector(reload), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 \(kAppName)",
                     action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        for it in menu.items where it.action != #selector(NSApplication.terminate(_:)) {
            it.target = self
        }
        item.menu = menu
    }

    @objc func showMainWindow() {
        NSApp.activate(ignoringOtherApps: true)
        // 从菜单栏恢复时窗口可能是最小化的 —— 只 orderFront 的话它还在 Dock 里
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
    }

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

    // ------------------------------------------------- 原生对话框 / 文件选择
    //
    // ⚠️⚠️ WKWebView **默认不实现** JS 的 alert / confirm / prompt,也不弹文件选择框。
    //    这不是"没样式",是**根本没有默认行为**:
    //      · 不实现 runJavaScriptConfirmPanel → `confirm()` 直接返回 false
    //        ⇒ 页面里每一处 `if(!confirm(...)) return;` 都永远提前返回。
    //          表现是删合集 / 删章节 / 删书点了**完全没反应,控制台一个错都不报**。
    //      · 不实现 runOpenPanel → `<input type="file">` **永远不弹框**
    //        ⇒ 上传封面点了没动静。
    //    这两个 bug 用浏览器或 Playwright 都测不出来 —— 只有 WKWebView 会这样。

    func webView(_ wv: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = kAppName
        a.informativeText = message
        a.addButton(withTitle: "好")
        if let w = window { a.beginSheetModal(for: w) { _ in completionHandler() } }
        else { a.runModal(); completionHandler() }
    }

    func webView(_ wv: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = kAppName
        a.informativeText = message
        a.addButton(withTitle: "确定")
        a.addButton(withTitle: "取消")
        if let w = window {
            a.beginSheetModal(for: w) { r in
                completionHandler(r == .alertFirstButtonReturn)
            }
        } else {
            completionHandler(a.runModal() == .alertFirstButtonReturn)
        }
    }

    func webView(_ wv: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = prompt
        a.addButton(withTitle: "确定")
        a.addButton(withTitle: "取消")
        let tf = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        tf.stringValue = defaultText ?? ""
        a.accessoryView = tf
        if let w = window {
            a.beginSheetModal(for: w) { r in
                completionHandler(r == .alertFirstButtonReturn ? tf.stringValue : nil)
            }
        } else {
            completionHandler(a.runModal() == .alertFirstButtonReturn ? tf.stringValue : nil)
        }
    }

    /// 「上传封面」走这里。不实现的话点按钮什么都不会发生。
    func webView(_ wv: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.allowedFileTypes = ["png", "jpg", "jpeg", "gif", "webp",
                                  "heic", "bmp", "tiff"]
        panel.allowsOtherFileTypes = true
        if let w = window {
            panel.beginSheetModal(for: w) { r in
                completionHandler(r == .OK ? panel.urls : nil)
            }
        } else {
            completionHandler(panel.runModal() == .OK ? panel.urls : nil)
        }
    }

    func webView(_ wv: WKWebView, didFinish navigation: WKNavigation!) { loading = false }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
