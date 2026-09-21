@echo off
rem ============================================================
rem  CEVTUO-RWP2EPUB —— Windows 启动器
rem
rem  和 macOS 版一样的目标:开一个**独立窗口**的应用,不是浏览器标签页。
rem  Windows 10/11 一定自带 Edge(Chromium 内核),用它的 --app 模式
rem  开一个没有地址栏的窗口,看起来就是原生应用,而且不需要任何编译。
rem
rem  Edge 同时也是渲染引擎 —— 跟 macOS 版连用户自己的浏览器是同一套思路。
rem ============================================================
setlocal enabledelayedexpansion
set "DIR=%~dp0"
set "PORT=8611"
set "URL=http://127.0.0.1:%PORT%"
set "DATA=%LOCALAPPDATA%\CEVTUO-RWP2EPUB"

if not exist "%DATA%\logs" mkdir "%DATA%\logs" 2>nul

rem ---- 已经在跑:直接开窗口走人 ----
curl -s -m 1 "%URL%/api/state" >nul 2>&1
if not errorlevel 1 goto open

rem ---- 找运行时:优先用自带的 ----
set "PY=%DIR%python\python.exe"
if not exist "%PY%" set "PY=%DIR%.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [CEVTUO-RWP2EPUB] 找不到 Python 运行时:
    echo   %DIR%python\python.exe
    echo 请重新解压安装包,不要只复制其中一部分文件。
    pause
    exit /b 1
)

set "PYTHONPATH=%DIR%pylibs"
set "CEVTUO_DATA_DIR=%DATA%"
set "PYTHONUNBUFFERED=1"

rem ---- 后台拉起本地服务 ----
start "" /b "%PY%" "%DIR%ui\server.py" --port %PORT% >>"%DATA%\logs\server.log" 2>&1

rem ---- 等就绪(最多 30 秒) ----
for /l %%i in (1,1,30) do (
    curl -s -m 1 "%URL%/api/state" >nul 2>&1
    if not errorlevel 1 goto open
    timeout /t 1 /nobreak >nul
)
echo [CEVTUO-RWP2EPUB] 本地服务启动失败,请看:
echo   %DATA%\logs\server.log
pause
exit /b 1

:open
rem Edge 优先,其次 Chrome;都用独立的窗口 profile,不去碰你日常浏览器的数据
set "BROWSER="
for %%P in (
    "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
    "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
    "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
) do (
    if not defined BROWSER if exist %%P set "BROWSER=%%~P"
)

if defined BROWSER (
    start "" "%BROWSER%" --app=%URL% --window-size=1380,900 ^
        --user-data-dir="%DATA%\shell" --no-first-run --no-default-browser-check
) else (
    rem 连 Edge 都没有(极罕见):退回默认浏览器
    start "" "%URL%"
)
exit /b 0
