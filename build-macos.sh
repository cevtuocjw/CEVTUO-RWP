#!/bin/bash
# 构建 CEVTUO-RWP 的 macOS .app + DMG。
#
# 为什么要有个脚本:上一次是手工拼的,所以「改了源码 → 装到你机器上的还是旧版」
# 这件事发生过不止一次 —— 因为没有任何一步会因为忘记同步而报错。
#
# 源码唯一真相是 ~/Library/Application Support/RSSDailyEpub(开发目录),
# 这个脚本负责把它同步进 .app 里的 Resources/app/。
set -euo pipefail

SRC="$HOME/Library/Application Support/RSSDailyEpub"
BUILD="$HOME/Library/Application Support/CEVTUO-RWP-build"
APPNAME="CEVTUO-RWP"
APP="/Applications/$APPNAME.app"
RAPP="$APP/Contents/Resources/app"
DIST="$BUILD/dist"

# 只同步会被改动的部分。python/ 和 pylibs/ 是平台相关的二进制,原样保留。
FILES=(browser.py capture.py export_epub.py extract_page.py fulltext.py
       imports.py jobs.py opml.py paths.py rss2epub.py run_scheduled.py
       sanitize.py scheduler.py store.py ui)

say(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

say "0. 前置检查"
[ -d "$SRC" ] || { echo "源码目录不存在: $SRC"; exit 1; }
[ -d "$RAPP" ] || { echo "app 包不存在,请先手工装配一次: $RAPP"; exit 1; }
echo "源码 $SRC"
echo "目标 $RAPP"

say "1. 同步源码 → app 包"
for f in "${FILES[@]}"; do
  if [ -d "$SRC/$f" ]; then
    rsync -a --delete "$SRC/$f/" "$RAPP/$f/"
  else
    rsync -a "$SRC/$f" "$RAPP/$f"
  fi
  echo "  $f"
done

say "2. 清掉 __pycache__"
# ⚠️ 必须清。Python 按 mtime 判断 .pyc 是否过期,而 rsync -a 会保留源文件的
#    时间戳 —— 源文件比 .pyc 旧的时候,解释器会继续用旧的字节码,
#    表现就是「改了代码但行为没变」,而且完全不报错。
find "$RAPP" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "  已清"

say "3. 顺手理掉旧名字的残留"
if [ -f "$RAPP/启动 CEVTUO-RWP2EPUB.command" ]; then
  mv "$RAPP/启动 CEVTUO-RWP2EPUB.command" "$RAPP/启动 $APPNAME.command"
  echo "  启动 CEVTUO-RWP2EPUB.command → 启动 $APPNAME.command"
fi

say "4. 重新签名(ad-hoc)"
codesign --force --deep --sign - "$APP"
codesign -dv "$APP" 2>&1 | grep -E "Identifier|Signature" | sed 's/^/  /'

say "5. 打 DMG"
mkdir -p "$DIST"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"      # 拖进去就能装
rm -f "$DIST/$APPNAME-macOS.dmg"
hdiutil create -volname "$APPNAME" -srcfolder "$STAGE" \
  -ov -format UDZO "$DIST/$APPNAME-macOS.dmg" >/dev/null
rm -rf "$STAGE"
ls -lh "$DIST/$APPNAME-macOS.dmg" | awk '{print "  "$9"  "$5}'

say "6. 同步进 repo 的工作副本"
for f in "${FILES[@]}"; do
  if [ -d "$SRC/$f" ]; then rsync -a --delete "$SRC/$f/" "$BUILD/$f/"
  else rsync -a "$SRC/$f" "$BUILD/$f"; fi
done
rsync -a "$BUILD/docs/" "$BUILD/docs/"   # no-op,保持目录存在
echo "  已同步(供 git 提交)"

printf '\n\033[32m完成。\033[0m app: %s\nDMG: %s\n' "$APP" "$DIST/$APPNAME-macOS.dmg"
