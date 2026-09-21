#!/bin/bash
# 认证好之后,双击/运行这一个脚本就能把仓库推上 GitHub。
set -e
cd "$(dirname "$0")"
echo "推到 $(git remote get-url origin)"
git push -u origin main
echo
echo "接下来在 GitHub 上做三件事(网页操作):"
echo "  1) Settings → Pages → Source 选 'Deploy from a branch',分支 main,目录 /docs"
echo "  2) Releases → Draft a new release,标签 v1.0,把 dist/CEVTUO-RWP2EPUB-macOS.dmg 传上去"
echo "  3) Actions → build-windows → Run workflow,产出 Windows 包后再挂到同一个 Release"
