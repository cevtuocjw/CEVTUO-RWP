# -*- coding: utf-8 -*-
"""程序目录 vs 数据目录。

源码可以待在 .app 包里(可能只读 —— 从 DMG 直接运行、或装在只读卷上),
而数据(数据库 / 输出 / 日志 / Chrome profile 副本)必须写在用户可写的地方。
所以这两者要分开:
  * CODE_DIR —— 源码与自带运行时所在,只读也能用
  * DATA_DIR —— 由环境变量 CEVTUO_DATA_DIR 指定,默认落到
                ~/Library/Application Support/CEVTUO-RWP2EPUB
没设环境变量时(开发期直接跑源码)两者重合,行为和以前完全一样。
"""
import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent

_DEFAULT_DATA = (Path.home() / "Library/Application Support/CEVTUO-RWP2EPUB")
_env = (os.environ.get("CEVTUO_DATA_DIR") or "").strip()
DATA_DIR = Path(_env).expanduser() if _env else (
    _DEFAULT_DATA if CODE_DIR.name == "app" else CODE_DIR)
# 说明:CODE_DIR.name == "app" 表示我们跑在 .app/Contents/Resources/app 里
# (打包形态),此时数据必须外置;否则就是在开发目录里直接跑,沿用原行为。

SUBDIRS = ("state", "output", "books", "logs", "history", "covers", "cookies")


def ensure():
    """确保数据目录及各子目录存在,返回数据目录。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return DATA_DIR


def seed(name):
    """首次运行时把程序自带的文件(如 feeds.opml)播种到数据目录。

    返回数据目录里的那个路径(即使源文件不存在,也返回目标路径)。
    """
    dst = DATA_DIR / name
    src = CODE_DIR / name
    try:
        if not dst.exists() and src.exists() and src.is_file():
            import shutil
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    except Exception:
        pass
    return dst
