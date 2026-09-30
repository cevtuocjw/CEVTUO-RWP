#!/bin/bash
# 重建 CEVTUO-RWP 的 Windows 包。
#
# 只替换**源码**部分 —— python\ 和 pylibs\ 是 Windows 平台的二进制,
# 在 macOS 上没法重新拉(lxml / Pillow 都是平台相关的),也不需要:
# 这次改的全是 .py 和 ui/。
#
# ⚠️ 打包必须用 Python 的 zipfile,不能用 `zip` 命令:
#    macOS 的 zip 写中文文件名时不带 UTF-8 标志位,Windows 资源管理器
#    会按 GBK 解释,「使用说明.txt」「停止服务.cmd」就变成乱码。
set -euo pipefail

SRC="$HOME/Library/Application Support/RSSDailyEpub"
BUILD="$HOME/Library/Application Support/CEVTUO-RWP-build"
WB="$BUILD/winbuild"
OLD="$WB/CEVTUO-RWP2EPUB"
NEW="$WB/CEVTUO-RWP"
OUT="$BUILD/dist/CEVTUO-RWP-windows.zip"

# ⚠️ 清单不能写死 —— 新增 coverart.py 时就是因为手写清单漏了它,
#    app 包里没有这个模块、启动直接 ModuleNotFoundError,而构建全绿。
FILES=($(cd "$SRC" && ls *.py 2>/dev/null | sort) ui windows)

say(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

say "0. 准备解压目录"
[ -d "$OLD" ] || { echo "缺少 $OLD,先解压一次原包"; exit 1; }
[ -d "$NEW" ] && rm -rf "$NEW"
cp -R "$OLD" "$NEW"
echo "  $OLD → $NEW"

say "1. 换成新名字"
[ -f "$NEW/CEVTUO-RWP2EPUB.cmd" ] && mv "$NEW/CEVTUO-RWP2EPUB.cmd" "$NEW/CEVTUO-RWP.cmd"
( cd "$NEW" && find . -name "*.cmd" -print0 | while IFS= read -r -d '' f; do
    grep -rl "CEVTUO-RWP2EPUB" "$f" >/dev/null 2>&1 && \
      perl -i -pe 's/CEVTUO-RWP2EPUB/CEVTUO-RWP/g' "$f" 2>/dev/null || true
  done )
echo "  $NEW/CEVTUO-RWP.cmd"

say "2. 同步源码"
for f in "${FILES[@]}"; do
  case "$f" in
    windows) continue ;;                      # windows/ 只用于生成启动器,不整目录塞进去
  esac
  [ -e "$SRC/$f" ] || continue
  if [ -d "$SRC/$f" ]; then rsync -a --delete "$SRC/$f/" "$NEW/$f/"
  else rsync -a "$SRC/$f" "$NEW/$f"; fi
  echo "  $f"
done

say "3. 放入 Windows 专用文件(启动器 / 说明 / 停止服务)"
for f in "$SRC/../CEVTUO-RWP-build/windows"/*; do
  [ -e "$f" ] || continue
  base="$(basename "$f")"
  case "$base" in
    CEVTUO-RWP2EPUB.cmd) cp "$f" "$NEW/CEVTUO-RWP.cmd" ;;
    *) cp "$f" "$NEW/$base" ;;
  esac
  echo "  $base"
done

say "4. 清 __pycache__"
find "$NEW" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "  已清"

say "5. 打包(UTF-8 文件名标志)"
mkdir -p "$BUILD/dist"
rm -f "$OUT"
python3 - "$WB" "$NEW" "$OUT" <<'PY'
import os, sys, zipfile
wb, root, out = sys.argv[1], sys.argv[2], sys.argv[3]
top = os.path.basename(root)
n = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.join(top, os.path.relpath(full, root))
            # zipfile 在文件名非 ASCII 时会自动置 UTF-8 标志位(bit 11)
            z.write(full, rel)
            n += 1
print(f"  写入 {n} 个文件")
PY
ls -lh "$OUT" | awk '{print "  "$9"  "$5}'

say "6. 验证包内文件与源文件一致"
python3 - "$SRC" "$OUT" <<'PY'
import sys, zipfile, hashlib, os
src, out = sys.argv[1], sys.argv[2]
top = "CEVTUO-RWP/"
check = ["store.py", "imports.py", "sanitize.py", "ui/server.py", "ui/index.html"]
bad = 0
with zipfile.ZipFile(out) as z:
    names = z.namelist()
    for f in check:
        want = hashlib.md5(open(os.path.join(src, f), "rb").read()).hexdigest()
        got = hashlib.md5(z.read(top + f)).hexdigest()
        ok = want == got
        bad += not ok
        print(f"  {'✓' if ok else '✗ 不一致'} {f}")
    # UTF-8 标志位:中文文件名必须置位,否则 Windows 上乱码
    cn = [i for i in z.infolist() if any(ord(c) > 127 for c in i.filename)]
    flagged = [i for i in cn if i.flag_bits & 0x800]
    print(f"  中文文件名 {len(cn)} 个,带 UTF-8 标志 {len(flagged)} 个")
    if cn and len(flagged) != len(cn):
        bad += 1; print("  ✗ 有中文名缺 UTF-8 标志,Windows 会乱码")
    if top + "CEVTUO-RWP.cmd" not in names:
        bad += 1; print("  ✗ 启动器 CEVTUO-RWP.cmd 不在包里")
    else:
        print("  ✓ 启动器 CEVTUO-RWP.cmd 在包里")
sys.exit(1 if bad else 0)
PY

printf '\n\033[32m完成。\033[0m %s\n' "$OUT"
