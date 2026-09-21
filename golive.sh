#!/bin/bash
# 授权完成后运行本脚本:建仓库 → 推送 → 开 Pages → 发 Release → 触发 Windows 构建
set -e
export PATH="$HOME/bin:$PATH"
REPO="$HOME/Library/Application Support/CEVTUO-RWP2EPUB-build"
cd "$REPO"
OWNER=cevtuo; NAME=CEVTUO-RWP2EPUB; FULL="$OWNER/$NAME"

echo "== 0. 登录状态 =="
gh auth status

echo "== 1. 创建仓库(已存在则跳过) =="
if gh repo view "$FULL" >/dev/null 2>&1; then
    echo "   已存在,跳过"
else
    gh repo create "$FULL" --public \
      --description "把网页 / 网站内链接 / RSS / Markdown 做成 EPUB。原生桌面应用,自带运行时,下载即用。author: cevtuo"
fi

echo "== 2. 推送 main =="
git push -u origin main

echo "== 3. 开启 GitHub Pages(源: main 分支 /docs) =="
gh api -X POST "repos/$FULL/pages" -f "source[branch]=main" -f "source[path]=/docs" 2>/dev/null \
  || gh api -X PUT "repos/$FULL/pages" -f "source[branch]=main" -f "source[path]=/docs"

echo "== 4. 发 Release 并上传 macOS 安装包 =="
gh release create v1.0 "dist/CEVTUO-RWP2EPUB-macOS.dmg" \
  --title "CEVTUO-RWP2EPUB v1.0" \
  --notes "首个版本。

- macOS 原生窗口应用(AppKit + WKWebView),安装包内置 Python 运行时与全部依赖,开箱即用
- 多条导入路径:多个网址 / 网站内链接 / RSS / Markdown
- 用你自己的浏览器渲染取全文
- RSS 每日成书与定时
- 无导出页数限制

macOS 首次打开请右键 → 打开(ad-hoc 签名,未经公证)。

author: cevtuo"

echo "== 5. 触发 Windows 构建流水线 =="
gh workflow run build-windows.yml 2>/dev/null || echo "   (workflow 需要先推送后才有,稍后可在 Actions 页手动跑)"

echo
echo "== 完成 =="
echo "仓库:   https://github.com/$FULL"
echo "主页:   $(gh api "repos/$FULL/pages" --jq .html_url 2>/dev/null || echo 'https://'"$OWNER"'.github.io/'"$NAME"'/')"
echo "Actions: https://github.com/$FULL/actions"
