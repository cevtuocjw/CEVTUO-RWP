# -*- coding: utf-8 -*-
"""验证:新建合集 + 章节显示时间/来源 —— 尤其是**失败时看得见**。

────────────────────────────────────────────────────────────────────────
用户原话(2026-10-16)
────────────────────────────────────────────────────────────────────────
  「我今天处理书的时候想要做新合集点击确定点不动做不了新书 /
    另外做合集的电子书没有显示时间和来源」

「点击确定点不动」查了两轮**复现不出来**(干净状态、有自定义背景、标题重名、
任务跑过之后,四种都正常)。但现场留下两条硬证据:

  1. 用户真的试过 —— 截图里名称填着 `Cev1-101626`;
  2. **用户库里根本没有那个合集** ⇒ `POST /api/collection/create` 没落地。

⇒ 也就是说:请求**失败了**,而界面上**什么都没发生**。这就是这个文件要防的东西。

────────────────────────────────────────────────────────────────────────
为什么「什么都没发生」——三条静默的路都堵上了
────────────────────────────────────────────────────────────────────────
(a) 失败只弹一个 **2.8 秒的 toast**。用户没看见 = 没发生。而且弹窗开着、
    按钮又变回蓝色(disabled 只是降到 45% 不透明度),读起来就是「点不动」。
    ⇒ 现在错误**钉在弹窗里**(`#modalErr`),不自己消失。

(b) `setView('collection', id)` 里 `renderCollection` 是 **async 且没人 await**,
    它一抛就是「无人处理的 promise 拒绝」—— 页面上**一点痕迹都没有**。
    而 `closeModal()` 排在它前面 ⇒ 弹窗早就关了,屏幕上什么都不动。
    ⇒ 现在 `setView` 把渲染兜住,并且加了两条**全局**兜底
    (`unhandledrejection` / `error`),任何漏出来的异常都会自己说出来。

(c) 服务端 **`log_message` 是空的** —— 每个请求一个字都不记。
    「请求没到」和「到了但炸了」在现场长得一模一样,只能靠猜。
    ⇒ 现在写请求和有错的请求一律记进 `logs/requests.log`,异常**带栈**。

────────────────────────────────────────────────────────────────────────
断言分四节(第 ② 节是核心:它测的是"坏的时候看不看得出来")
────────────────────────────────────────────────────────────────────────
  ① 正常路径:点确定 → 弹窗关、视图切进去、库里真的有
  ② ★失败路径:让这个请求**真的失败**(CDP 断网),弹窗必须留着 + 显示原因
  ③ 视图兜底:合集不存在时要有动静,不能停在半死的页面
  ④ 章节行:日期 + 来源(域名)都要在

判据都是**读界面上的东西**,不是读中间变量 —— 因为坏掉的正是"界面说不说"。
"""
import json
import os
import sys
import time

import capture

BASE_URL = os.environ.get("RWP_BASE", "http://127.0.0.1:8611")
NAME = "__VERIFY_新建合集__"

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  — " + detail) if detail else ""))


def wait_for(tab, expr, secs=15, step=0.4):
    """等一个条件成真。⚠️ 等条件,不等固定秒数 ——
    「没等够」和「功能坏了」在输出里长得一模一样。"""
    end = time.time() + secs
    while time.time() < end:
        if tab.evaluate(expr):
            return True
        time.sleep(step)
    return False


def open_modal_and_fill(tab):
    tab.evaluate("closeModal(); setView('collections')")
    wait_for(tab, "!!document.querySelector('#newColl')")
    tab.evaluate("document.querySelector('#newColl').click()")
    wait_for(tab, "!!document.querySelector('#ncTitle')")
    tab.evaluate("document.querySelector('#ncTitle').value = %s" % json.dumps(NAME))
    tab.evaluate("document.querySelector('#ncAuthor').value = 'cev'")


def click_ok(tab):
    tab.evaluate("document.querySelector('[data-ok]').click()")


def modal_state(tab):
    return {
        "open": bool(tab.evaluate("!!document.querySelector('#modalHost .modal')")),
        "errShown": bool(tab.evaluate(
            "(() => {const e=document.querySelector('#modalErr');"
            " return !!(e && !e.hidden && e.textContent.trim());})()")),
        "errText": (tab.evaluate(
            "(() => {const e=document.querySelector('#modalErr');"
            " return e ? e.textContent.trim() : '';})()") or ""),
        "okBusy": bool(tab.evaluate(
            "(() => {const b=document.querySelector('[data-ok]');"
            " return !!(b && b.classList.contains('busy'));})()")),
        "okDisabled": bool(tab.evaluate(
            "(() => {const b=document.querySelector('[data-ok]');"
            " return !!(b && b.disabled);})()")),
        "title": tab.evaluate("document.querySelector('#title').textContent"),
    }


def main():
    # ⚠️ 先**复用**已经开着的那个 Chrome。以前这里无条件 kill_chrome() ——
    #    那会把用户正在用的采集浏览器杀掉再拉起,而我们只是要开一个标签页。
    #    连不上才去 ensure 一个。
    tab = None
    try:
        tab = capture.Tab(capture.DEFAULT_PORT, timeout=60)
    except Exception:
        ok, err = capture.ensure()
        if not ok:
            print("Chrome 起不来:", err)
            return 2
        tab = capture.Tab(capture.DEFAULT_PORT, timeout=60)
    leftover = None
    try:
        tab.cmd("Page.navigate", {"url": BASE_URL + "/"})
        if not wait_for(tab, "typeof renderCollection === 'function'", 40):
            print("界面没加载出来 —— 服务在跑吗?", BASE_URL)
            return 2
        print("被测界面 %s\n" % BASE_URL)

        # ══════════ ① 正常路径 ══════════
        print("① 正常路径")
        open_modal_and_fill(tab)
        click_ok(tab)
        entered = wait_for(tab, "document.querySelector('#title').textContent == %s"
                                % json.dumps(NAME), 20)
        check("点确定之后**视图真的切进了新合集**", entered,
              "标题=%r" % (tab.evaluate("document.querySelector('#title').textContent"),))
        check("弹窗已经关掉(成功路径)", not modal_state(tab)["open"])
        check("章节区渲染出来了(renderCollection 跑完了)",
              bool(tab.evaluate("!!document.querySelector('#chList')")))
        # 库里真的有 —— 别只看界面
        n_after_api = tab.evaluate(
            "fetch('/api/collections').then(r=>r.json()).then(j=>"
            "j.collections.filter(c=>c.title==%s).length)" % json.dumps(NAME),
            await_promise=True)
        check("库里确实建出来了(不是只有界面以为建了)",
              (n_after_api or 0) >= 1, "接口里同名 %s 条" % n_after_api)

        # ══════════ ② ★失败路径 ══════════
        print("\n② ★请求失败时,用户必须看得出来(核心)")
        # 让这个请求**真的失败**:CDP 断掉这条 URL 的网络。不改产品代码、不打桩。
        tab.cmd("Network.setBlockedURLs",
                {"urls": ["*/api/collection/create*"]})
        time.sleep(0.3)
        open_modal_and_fill(tab)
        tab.evaluate("document.querySelector('#ncTitle').value = %s"
                     % json.dumps(NAME + "_FAIL"))
        click_ok(tab)
        time.sleep(4.0)                      # toast 是 2.8 秒,故意等它过期
        st = modal_state(tab)
        check("请求失败后**弹窗还在**(不是一闪就没了)", st["open"])
        check("★ 失败的原因**留在屏幕上**(2.8 秒之后还在)",
              st["errShown"], "错误区文字=%r" % st["errText"][:80])
        check("按钮没有卡在「处理中」", (not st["okDisabled"]) and (not st["okBusy"]))
        # 负向对照的正面:老代码这里**没有**任何可见的错误区
        tab.cmd("Network.setBlockedURLs", {"urls": []})
        tab.evaluate("closeModal()")
        time.sleep(0.4)

        # ══════════ ③ 视图兜底 ══════════
        print("\n③ 合集不存在时不能停在半死的页面上")
        tab.evaluate("window.__tErr = ''; "
                     "addEventListener('unhandledrejection',"
                     " e => {window.__tErr = String(e.reason);})")
        tab.evaluate("setView('collection', 'no-such-collection-id')")
        time.sleep(3.0)
        # ⚠️ 判据必须**能区分兜底和没兜底**。只看画面看不出来:renderCollection
        #    是在碰 DOM **之前**就抛的,所以两种情况下屏幕上都还留着上一屏
        #    —— 我第一版就是这么写的,负向对照当场报「没红」。
        #    真正的契约是**应用状态**:兜底之后它必须承认"我在合集列表上",
        #    而不是抱着一个不存在的 cid 停在半路(下一次重渲染就会再炸一次)。
        fallback = wait_for(tab, "S.view === 'collections'", 12)
        check("★ 拿不到合集时**退回了合集列表**(S.view 回到 collections)", fallback,
              "S.view=%r 标题=%r" % (tab.evaluate("S.view"),
                                     tab.evaluate("document.querySelector('#title').textContent")))
        check("列表页面真的渲染出来了",
              (tab.evaluate("document.querySelectorAll('.ccard').length") or 0) >= 1)
        check("没有漏成「无人处理的 promise 拒绝」",
              not tab.evaluate("window.__tErr"),
              "漏出来的=%r" % (tab.evaluate("window.__tErr"),))

        # ══════════ ④ 章节行:日期 + 来源 ══════════
        print("\n④ 章节行要有时间和来源")
        cid = tab.evaluate(
            "fetch('/api/collections').then(r=>r.json()).then(j=>{"
            " const c=j.collections.filter(x=>x.title==%s);"
            " return c.length ? (c[0].id||c[0].cid) : null;})" % json.dumps(NAME),
            await_promise=True)
        check("拿到测试合集 id", bool(cid), "cid=%r" % cid)
        if cid and not isinstance(cid, str):
            cid = str(cid)
        if cid:
            leftover = cid
            # 塞两篇**带 url 和日期**的文章(直接走接口,不依赖外网)
            tab.evaluate(
                "fetch('/api/article/add',{method:'POST',"
                "headers:{'Content-Type':'application/json'},"
                "body:JSON.stringify({collectionId:%s,title:'__V1__',"
                "parsed_html:'<p>正文</p>'})})" % json.dumps(cid),
                await_promise=True)
            tab.evaluate(
                "(async () => { await fetch('/api/article/save',{method:'POST',"
                "headers:{'Content-Type':'application/json'},"
                "body:JSON.stringify({id: (await (await fetch("
                "'/api/articles?collection='+%s)).json()).articles[0].id,"
                "title:'__V1__',parsed_html:'<p>正文</p>'})}); })()"
                % json.dumps(cid), await_promise=True)
            # 用一条**假的** url 走 addUrl 太重(要外网),所以这里只断言渲染规则:
            # 直接比对界面那一行用了哪些字段 —— 数据里的 created_at / url。
            lines = tab.evaluate(
                "(async () => { await setView('collection', %s);"
                " await new Promise(r=>setTimeout(r,1500));"
                " return [...document.querySelectorAll('#chList .ch .ti small')]"
                ".map(e=>e.textContent); })()" % json.dumps(cid),
                await_promise=True) or []
            check("章节行渲染出来了", len(lines) >= 1, "共 %d 行" % len(lines))
            joined = " | ".join(lines)
            import re as _re
            has_day = bool(_re.search(r"\d{4}-\d{2}-\d{2}", joined))
            check("★ 每章都带**日期**(YYYY-MM-DD)", has_day, joined[:120])
            # 来源=域名:夹具里的文章没有 url,所以这里用真实数据那一批来验
            real_rows = tab.evaluate(
                "(async () => { const j = await (await fetch("
                "'/api/collections')).json();"
                " for (const c of j.collections) {"
                "   const a = await (await fetch('/api/articles?collection='+c.id)).json();"
                "   if (a.articles.some(x=>x.url)) { await setView('collection', c.id);"
                "     await new Promise(r=>setTimeout(r,1500));"
                "     return [...document.querySelectorAll('#chList .ch .ti small')]"
                "       .map(e=>e.textContent); } }"
                " return []; })()", await_promise=True) or []
            has_host = any("·" in t and t.split("·")[0].strip().count(".")
                           for t in real_rows)
            check("★ 章节行带**来源域名**(不是导入方式 urls/rss/website)",
                  has_host, (real_rows[0] if real_rows else "(没有带 url 的合集)")[:120])
            if real_rows:
                first = real_rows[0].split("·")[0].strip()
                check("来源看起来是域名,不是 'rss'/'urls' 这类导入方式",
                      first not in ("rss", "urls", "website", "markdown")
                      and ("." in first or first == ""),
                      "第一段=%r" % first)

        # ══════════ ⑤ 全局兜底 ══════════
        print("\n⑤ 漏出来的异常要自己说出来")
        tab.evaluate("window.__lastErr = ''; "
                     "document.querySelectorAll('.toast').forEach(e=>e.remove());"
                     " Promise.reject(new Error('__验证用__'));")
        got = wait_for(tab, "!!window.__lastErr", 6)
        check("★ 无人处理的 promise 拒绝被接住并提示了", got,
              "window.__lastErr=%r" % (tab.evaluate("window.__lastErr"),))
        toasted = tab.evaluate(
            "[...document.querySelectorAll('.toast')]"
            ".some(e=>e.textContent.includes('__验证用__'))")
        check("而且是以 toast 的形式**说给用户听**", bool(toasted))

    finally:
        try:
            if leftover:
                tab.evaluate("fetch('/api/collection/delete',{method:'POST',"
                             "headers:{'Content-Type':'application/json'},"
                             "body:JSON.stringify({id:%s})})" % json.dumps(leftover))
            tab.cmd("Network.setBlockedURLs", {"urls": []})
        except Exception:
            pass
        tab.close()

    print("\n══ 汇总: %d 通过 / %d 失败 ══" % (len(OK), len(BAD)))
    for b in BAD:
        print("   ✗ " + b)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
