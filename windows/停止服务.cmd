@echo off
rem ============================================================
rem  Stop the CEVTUO-RWP2EPUB local service.
rem
rem  Closing the app window leaves the local service running in
rem  the background (that is why reopening is instant).
rem  Double-click this file to shut it down completely.
rem
rem  NOTE: this file is intentionally ASCII-only. cmd.exe parses
rem  .cmd files using the OEM code page (936/GBK on Chinese
rem  Windows), so non-ASCII text here would be mis-parsed.
rem ============================================================
setlocal

echo.
echo   Stopping the CEVTUO-RWP2EPUB local service...
echo.

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*ui\server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo   Done.
echo.
timeout /t 3 /nobreak >nul
