@echo off
rem ============================================================
rem  停止 CEVTUO-RWP2EPUB 的本地服务
rem
rem  关掉浏览器窗口后,本地服务仍在后台运行(所以再次打开是秒开)。
rem  双击本文件可以把它彻底关掉。
rem ============================================================
chcp 65001 >nul 2>&1
setlocal

echo.
echo   Stopping the CEVTUO-RWP2EPUB local service...
echo   正在停止 CEVTUO-RWP2EPUB 本地服务...
echo.

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*ui\server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo   Done.  完成。
echo.
timeout /t 3 /nobreak >nul
