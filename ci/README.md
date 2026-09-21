# Windows 构建流水线

`build-windows.yml` 只有放在 `.github/workflows/` 下才会被 GitHub Actions 识别。
暂时放在这里,是因为推送它的 token 需要 `workflow` 权限,而当前登录的 token 只有
`gist, read:org, repo`,GitHub 会直接拒绝:

```
refusing to allow an OAuth App to create or update workflow
`.github/workflows/build-windows.yml` without `workflow` scope
```

## 启用方式(二选一)

**方式一 · 命令行**(需要再走一次设备授权,约 30 秒)

```bash
gh auth refresh -s workflow
mkdir -p .github/workflows
git mv ci/build-windows.yml .github/workflows/
git commit -m "启用 Windows 构建流水线" && git push
```

**方式二 · 网页**(不用改 token 权限,推荐)

在仓库页面 → **Add file → Upload files**,把 `ci/build-windows.yml`
上传到 `.github/workflows/` 目录下即可。

启用后在 **Actions** 页手动 Run workflow,产出的 zip 挂到 Release 上,
下载页的 Windows 按钮就能用了。
