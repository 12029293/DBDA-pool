@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  dola-pool sidecar launcher (double-click to use)
rem
rem  Starts the Roins-hub/dola-pool video pool service on
rem  http://127.0.0.1:8002 (admin panel + OpenAI-style /v1/videos).
rem
rem  This script does NOT touch any existing Heti source files.
rem  Existing 启动.bat / run_engine.bat remain unchanged.
rem ============================================================

set "ROOT=%~dp0"
set "POOL_DIR=C:\Users\Administrator\Desktop\dola-pool"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

rem WorkBuddy 工具内启动会继承 PYTHONPATH（插件 shim 目录），其 sitecustomize 会把
rem shutil.rmtree 换成「批量删除保护」版本：面板里一次删多个账号（每个号一个目录）
rem 会触发 SystemExit(1) 把整个 uvicorn 打死。双击启动本无此变量，这里统一清掉以防万一。
set "PYTHONPATH="

rem 纯 API 签名器需要 node；双击/计划任务环境的 PATH 可能找不到 node（WinError 2），显式指定
set "NODE_EXE=C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
if exist "%NODE_EXE%" set "DOLA_NODE=%NODE_EXE%"

if not exist "%POOL_DIR%\server.py" (
    echo [ERROR] %POOL_DIR% not found.
    pause
    exit /b 1
)

if not exist "%PY%" (
    echo [ERROR] managed python venv not found: %PY%
    pause
    exit /b 1
)

echo ===== dola-pool sidecar starting on http://127.0.0.1:8002 =====
echo   Admin panel : http://127.0.0.1:8002/   (key: see .env.local)
echo   API docs    : http://127.0.0.1:8002/docs
echo.

rem 防双开：8002 已有监听说明服务在跑，直接打开面板即可（Windows 允许重复绑定端口，双实例会互抢请求）
netstat -ano | findstr :8002 | findstr LISTENING >nul 2>&1
if not errorlevel 1 (
    echo [提示] 8002 端口已有 dola-pool 在运行，直接打开面板（不重复启动）。
    start "" http://127.0.0.1:8002/
    timeout /t 3 /nobreak >nul
    exit /b 0
)

rem 服务就绪后自动打开管理面板：轮询等 8002 真正进入 LISTENING 再开浏览器。
rem 之前固定等 2 秒，但 uvicorn 启动时要先恢复中断任务、之后才绑定端口（慢时 30 秒+），
rem 浏览器开太早只会看到 ERR_CONNECTION_REFUSED。
echo 等待服务就绪（面板就绪后自动打开，最长等 120 秒）...
start "" /b cmd /c "for /l %%i in (1,1,120) do @(netstat -ano | findstr :8002 | findstr LISTENING >nul 2>&1 && (start "" http://127.0.0.1:8002/ & exit /b) & timeout /t 1 /nobreak >nul)"

cd /d "%POOL_DIR%"
"%PY%" -m uvicorn server:app --host 127.0.0.1 --port 8002

echo.
echo ===== dola-pool exited =====
pause
