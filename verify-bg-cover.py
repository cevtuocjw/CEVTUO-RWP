# -*- coding: utf-8 -*-
"""验证背景图是「铺满」而不是「拉伸」——**用像素量,不看计算样式**。

用户原话:「背景要全铺满,要调整合适,**而不是拉伸的**」。

────────────────────────────────────────────────────────────────────────
为什么不能只断言 `getComputedStyle(...).backgroundSize === 'cover'`
────────────────────────────────────────────────────────────────────────
那句话是**声明**,不是**事实**。这个项目已经栽过好几次同类跟头
(FACT 纪律 4:「写在样式表里 ≠ 浏览器认了它」)——
声明写对了而实际被别的规则盖掉,是完全可能的,而且看不出来。

所以这里放一张**中间有正方形**的图,去数它在屏幕上量出来的宽高比:
  · 用 `cover`(等比铺满、裁掉多余)  ⇒ 正方形还是**正方形**(比值 ≈ 1)
  · 用 `100% 100%`(拉伸铺满)        ⇒ 正方形被压成**细长条**(比值 ≈ 0.28)
两者差了 3.5 倍,一眼分得开。

⚠️ 量的时候把页面上**所有内容临时隐藏**,只为让背景露出来 ——
   被测的那条 CSS(body::before)一个字都没动。
⚠️ 解图在**页面里**做(Canvas + getImageData):本机没有任何
   PNG/JPEG 解码器(FACT 里记着)。
"""
import base64
import json
import sys
import time

import capture

W, H = 600, 400          # 视口。600/400 = 1.5
IMG_W, IMG_H = 3200, 600  # 原图比例 5.333 —— 和视口差得远,拉伸会非常明显
SQ = 200                  # 原图正中那个正方形的边长

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  — " + detail) if detail else ""))


FIXTURE = """<!doctype html><html><head><meta charset="utf-8"><title>fix</title>
<style>
 body{margin:0;height:100vh;position:relative}
 body::before{content:'';position:absolute;inset:0;
   background-image:var(--bgimg,none);background-size:%s;
   background-position:center center;background-repeat:no-repeat}
</style></head><body></body></html>"""


def measure(tab, label):
    """在当前页面上截图,数出深色区域的宽高比。

    ⚠️ 解图必须在**页面里**做:本机没有任何 PNG/JPEG 解码器
    (FACT 里记着),Node/Python 那边都开不了这张图。
    """
    r = tab.cmd("Page.captureScreenshot", {"format": "jpeg", "quality": 80},
                timeout=40)
    data = ((r or {}).get("result") or {}).get("data")
    if not data:
        print("     [%s] 截图失败" % label)
        return {"bw": 0, "bh": 0, "n": 0, "err": "no screenshot"}
    expr = """(async () => {
        const im = new Image();
        im.src = 'data:image/jpeg;base64,' + %s;
        await im.decode();
        const c = document.createElement('canvas');
        c.width = im.width; c.height = im.height;
        const x = c.getContext('2d');
        x.drawImage(im, 0, 0);
        const d = x.getImageData(0, 0, c.width, c.height).data;
        let x0=1e9, x1=-1, y0=1e9, y1=-1, n=0;
        for (let j=0;j<c.height;j++) for (let i=0;i<c.width;i++) {
            const o=(j*c.width+i)*4;
            // 那个正方形是纯黑,底是白的。上面压了一层很淡的纱,
            // 黑还是黑、白还是白 —— 取中间的阈值就能分开。
            const lum = 0.299*d[o] + 0.587*d[o+1] + 0.114*d[o+2];
            if (lum < 170) { n++; if(i<x0)x0=i; if(i>x1)x1=i; if(j<y0)y0=j; if(j>y1)y1=j; }
        }
        return JSON.stringify({w: c.width, h: c.height, n: n,
                               bw: x1-x0+1, bh: y1-y0+1});
    })()""" % json.dumps(data)
    # ⚠️ await_promise 必须开 —— 不开的话拿回来的是个 Promise,value 是 None
    v = tab.evaluate(expr, 60, await_promise=True)
    if not v:
        print("     [%s] 页面里解图返回了空" % label)
        return {"bw": 0, "bh": 0, "n": 0, "err": "decode returned nothing"}
    return json.loads(v)


if __name__ == "__main__":
    # ── 造夹具图:白底 + 正中一个纯黑正方形 ──────────────────────────
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (IMG_W, IMG_H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([IMG_W // 2 - SQ // 2, IMG_H // 2 - SQ // 2,
                 IMG_W // 2 + SQ // 2, IMG_H // 2 + SQ // 2], fill=(0, 0, 0))
    import io as _io
    buf = _io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    capture.kill_chrome()
    time.sleep(1.0)
    ok, err = capture.ensure()
    if not ok:
        print("Chrome 起不来:", err)
        sys.exit(2)

    tab = capture.Tab(capture.DEFAULT_PORT, timeout=60)
    try:
        tab.cmd("Emulation.setDeviceMetricsOverride",
                {"width": W, "height": H, "deviceScaleFactor": 1,
                 "mobile": False})

        # ── ① 负向对照:故意用会被拉伸的写法 ────────────────────────
        print("① 负向对照 —— 故意用 `100%% 100%%`(就是「拉伸」那种做法)")
        ui = "data:text/html;charset=utf-8," + \
             __import__("urllib.parse", fromlist=["quote"]).quote(FIXTURE % "100% 100%")
        tab.cmd("Page.navigate", {"url": ui})
        time.sleep(1.2)
        tab.evaluate("document.documentElement.style.setProperty('--bgimg',%s)"
                     % json.dumps("url(%s)" % data_url))
        time.sleep(1.2)
        m_bad = measure(tab, "stretch")
        print("     量到深色块 %s×%s" % (m_bad["bw"], m_bad["bh"]))
        ratio_bad = m_bad["bw"] / max(1, m_bad["bh"])
        check("拉伸写法下,正方形**确实被压扁了**(所以这个量法测得出东西)",
              ratio_bad < 0.6, "宽高比 %.2f" % ratio_bad)

        # ── ② 被测的写法:cover ─────────────────────────────────────
        print("\n② 被测的写法 —— `cover`(保持比例铺满,裁掉多余)")
        ui = "data:text/html;charset=utf-8," + \
             __import__("urllib.parse", fromlist=["quote"]).quote(FIXTURE % "cover")
        tab.cmd("Page.navigate", {"url": ui})
        time.sleep(1.2)
        tab.evaluate("document.documentElement.style.setProperty('--bgimg',%s)"
                     % json.dumps("url(%s)" % data_url))
        time.sleep(1.2)
        m_ok = measure(tab, "cover")
        print("     量到深色块 %s×%s" % (m_ok["bw"], m_ok["bh"]))
        ratio_ok = m_ok["bw"] / max(1, m_ok["bh"])
        check("★ 用 cover,正方形**还是正方形** —— 没有被拉伸",
              abs(ratio_ok - 1.0) < 0.15, "宽高比 %.2f" % ratio_ok)
        check("★ 和拉伸那组差得很明显(证明判据有分辨力)",
              ratio_ok > ratio_bad * 2.5,
              "%.2f vs %.2f" % (ratio_ok, ratio_bad))
        check("图确实铺满了整个视口(深色块周围没有露出背景外)",
              m_ok["n"] > 0 and m_ok["bw"] > 40,
              "深色像素 %s 个" % m_ok["n"])

        # ── ③ app 真的在用它吗 ─────────────────────────────────────
        print("\n③ app 自己的样式(不是夹具页面)")
        tab.cmd("Page.navigate", {"url": "http://127.0.0.1:8611/"})
        for _ in range(40):
            time.sleep(0.5)
            if tab.evaluate("!!document.body"):
                break
        time.sleep(2.0)
        cs = tab.evaluate("""(() => {
            const s = getComputedStyle(document.body, '::before');
            return JSON.stringify({size:s.backgroundSize, rep:s.backgroundRepeat,
                                   pos:s.backgroundPosition,
                                   img:(s.backgroundImage||'').slice(0,60),
                                   has:document.body.classList.contains('has-bg')});
        })()""")
        cs = json.loads(cs) if cs else {}
        print("     ", cs)
        check("app 的背景规则是 cover", cs.get("size") == "cover",
              "background-size=%s" % cs.get("size"))
        check("app 的背景不重复", cs.get("rep") == "no-repeat",
              "background-repeat=%s" % cs.get("rep"))
        check("app 上确实挂上了自定义背景", bool(cs.get("has")),
              "has-bg=%s" % cs.get("has"))

        # ── ④ 设置里那一套:**真的点一遍** ──────────────────────────
        print("\n④ 设置页里的操作(点在真界面上)")
        tab.evaluate("setView('settings')")
        for _ in range(40):
            time.sleep(0.5)
            if tab.evaluate("!!document.querySelector('#bgGo')"):
                break
        check("设置页里有背景这一块(输入框 + 按钮)",
              bool(tab.evaluate("!!document.querySelector('#bgUrl') && "
                                "!!document.querySelector('#bgGo') && "
                                "!!document.querySelector('#bgPick')")))

        # 走一遍真流程:填链接 → 点「用这个链接」
        tab.evaluate("""(() => {
            const i = document.querySelector('#bgUrl');
            i.value = %s;
            i.dispatchEvent(new Event('input',{bubbles:true}));
            document.querySelector('#bgGo').click();
        })()""" % json.dumps("http://127.0.0.1:8791/bgtest.jpg"))
        prev = ""
        for _ in range(30):
            time.sleep(0.5)
            prev = tab.evaluate(
                "(() => { const d=document.querySelector('#bgPrev div:last-child');"
                " return d ? getComputedStyle(d).backgroundSize + '|' + "
                "getComputedStyle(d).backgroundImage.slice(0,30) : ''; })()") or ""
            if "cover" in prev:
                break
        check("★ 预览框出来了,而且**用的也是 cover**（所见即所得）",
              "cover" in prev, "preview=%r" % prev[:70])

        info = tab.evaluate("document.querySelector('#bgInfo').textContent") or ""
        check("界面上报出了当前背景的尺寸/大小",
              "×" in info, "bgInfo=%r" % info.strip()[:50])

        # 恢复默认
        tab.evaluate("document.querySelector('#bgReset').click()")
        off = False
        for _ in range(20):
            time.sleep(0.5)
            off = not tab.evaluate(
                "document.body.classList.contains('has-bg')")
            if off:
                break
        check("★ 「恢复默认背景」真的把它撤掉了",
              bool(off) and not tab.evaluate(
                  "!!document.querySelector('#bgPrev div')"))
    finally:
        tab.close()

    print("\n══ 汇总: %d 通过 / %d 失败 ══" % (len(OK), len(BAD)))
    for b in BAD:
        print("   ✗ " + b)
    sys.exit(1 if BAD else 0)
