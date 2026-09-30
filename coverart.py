# -*- coding: utf-8 -*-
"""封面绘制 —— 把**书名和作者写在图上**;没有图就给一张只有书名作者的。

用户的要求:
  · 不论用哪种方式做封面(上传本地图 / 从链接取图),都要把书名和作者写在图片上面
  · 没有封面,那就只写书名和作者

⚠️ 为什么要专门写这个:封面在阅读器里是**缩略图**,一张没有文字的风景照
和另一本没有文字的风景照长得一模一样。书名写在图上,书架上才认得出哪本是哪本。
"""
import io
import os

# 中文字体。macOS / Windows / Linux 各找一个,找不到就退回 PIL 内置位图字体
# (那玩意儿不支持中文,所以能找就一定要找到)。
_FONT_PATHS = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)

_font_cache = {}


def font(size):
    """按字号取一个字体对象(带缓存 —— truetype 每次调用都要读一遍文件)。"""
    size = max(8, int(size))
    if size in _font_cache:
        return _font_cache[size]
    from PIL import ImageFont
    f = None
    for p in _FONT_PATHS:
        if not os.path.exists(p):
            continue
        for idx in (0, 1):
            try:
                f = ImageFont.truetype(p, size, index=idx)
                break
            except Exception:
                continue
        if f:
            break
    if f is None:
        try:
            f = ImageFont.load_default(size)
        except Exception:
            f = ImageFont.load_default()
    _font_cache[size] = f
    return f


_CJK = lambda ch: ("\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef"
                   or "\u4e00" <= ch <= "\u9fff")


def wrap(draw, text, f, max_w, max_lines=3):
    """按宽度折行。中文按字断,西文按词断。

    ⚠️ 不能直接按字符数切:同样 20 个字符,"WWWW" 比 "iiii" 宽一倍。
    必须用 textlength 真量。
    """
    text = (text or "").strip()
    if not text:
        return []
    units, buf = [], ""
    for ch in text:
        if ch == " ":
            if buf:
                units.append(buf)
                buf = ""
            units.append(" ")
        elif _CJK(ch):
            if buf:
                units.append(buf)
                buf = ""
            units.append(ch)
        else:
            buf += ch
    if buf:
        units.append(buf)

    lines, cur = [], ""
    for u in units:
        trial = cur + u
        if cur and draw.textlength(trial, font=f) > max_w:
            lines.append(cur.rstrip())
            cur = "" if u == " " else u
            if len(lines) >= max_lines:
                break
        else:
            cur = trial
    if cur.strip() and len(lines) < max_lines:
        lines.append(cur.rstrip())
    if len(lines) >= max_lines:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=f) > max_w:
            last = last[:-1]
        lines[-1] = (last + "…") if len("".join(lines)) < len(text) else last
    return lines


def _scrim(size, start=0.40, strength=232):
    """底部由透明渐变到近黑的遮罩 —— 保证白字在任何图上都读得清。"""
    from PIL import Image
    W, H = size
    col = Image.new("L", (1, H), 0)
    y0 = int(H * start)
    span = max(1, H - y0)
    for y in range(y0, H):
        t = (y - y0) / span
        col.putpixel((0, y), int(strength * min(1.0, t ** 1.15)))
    return col.resize((W, H))


def compose(img_bytes, title, author, size=(900, 1350)):
    """把书名/作者压在一张图的下方,返回 JPEG 字节。"""
    from PIL import Image, ImageOps
    W, H = size
    im = Image.open(io.BytesIO(img_bytes))
    im = im.convert("RGB")
    im = ImageOps.fit(im, size, Image.LANCZOS)
    scrim = _scrim(size)
    im = Image.composite(Image.new("RGB", size, (6, 8, 12)), im, scrim)
    im = _render(im, title, author, size)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def generate(title, author, size=(900, 1350)):
    """完全没有封面时生成一张 —— 只有书名和作者。"""
    from PIL import Image, ImageDraw
    W, H = size
    im = Image.new("RGB", size, (26, 30, 40))
    d = ImageDraw.Draw(im)
    for y in range(H):                       # 竖向渐变,比纯色体面
        t = y / max(1, H - 1)
        d.line([(0, y), (W, y)],
               fill=(int(28 + 46 * t), int(32 + 50 * t), int(44 + 66 * t)))
    im = _render(im, title, author, size)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def _render(im, title, author, size):
    """在图上居中画书名 + 作者(底部对齐,像正经书封)。"""
    from PIL import ImageDraw
    W, H = size
    d = ImageDraw.Draw(im)
    pad = int(W * 0.08)
    max_w = W - pad * 2
    tf = font(int(W * 0.088))
    af = font(int(W * 0.047))
    tlines = wrap(d, title or "未命名", tf, max_w, 3)
    lh = int(tf.size * 1.3)
    y = H - int(H * 0.085) - len(tlines) * lh - (int(af.size * 2.0) if author else 0)

    dx = max(1, int(W * 0.0045))
    for ln in tlines:
        w = d.textlength(ln, font=tf)
        d.text(((W - w) / 2, y), ln, font=tf, fill=(255, 255, 255),
               stroke_width=dx, stroke_fill=(0, 0, 0))
        y += lh
    a = (author or "").strip()
    if a:
        y += int(af.size * 0.45)
        aw = d.textlength(a, font=af)
        d.text(((W - aw) / 2, y), a, font=af, fill=(226, 228, 236),
               stroke_width=max(1, dx - 1), stroke_fill=(0, 0, 0))
    return im
