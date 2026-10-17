@echo off
setlocal

rem ============================================================
rem  dola-pool startup (double-click)
rem  Panel:  http://127.0.0.1:8019/
rem  Docs :  http://127.0.0.1:8019/docs
rem ============================================================

set "POOL_DIR=C:\Users\Administrator\Desktop\DBDA-pool\DBDA-pool-main"
set "PY=C:\Users\Administrator\Desktop\DBDA-pool\DBDA-pool-main\.venv\Scripts\python.exe"

set "PYTHONPATH="

if not exist "%POOL_DIR%\server.py" (
    echo [ERROR] server.py not found in %POOL_DIR%
    pause
    exit /b 1
)

if not exist "%PY%" (
    echo [ERROR] venv python not found: %PY%
    pause
    exit /b 1
)

echo ===== dola-pool starting on http://127.0.0.1:8019 =====
echo   Admin panel : http://127.0.0.1:8019/
echo   API docs    : http://127.0.0.1:8019/docs
echo.

rem ---- already running? just open the panel ----
netstat -ano | findstr :8019 | findstr LISTENING >nul 2>&1
if not errorlevel 1 (
    echo [INFO] dola-pool already running on port 8019, opening panel...
    start "" http://127.0.0.1:8019/
    timeout /t 3 /nobreak >nul
    exit /b 0
)

rem ---- open panel automatically once port 8019 is listening (max 120s) ----
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 120;$i++){ Start-Sleep -Seconds 1; $c=Get-NetTCPConnection -LocalPort 8019 -State Listen -ErrorAction SilentlyContinue; if($c){ Start-Process 'http://127.0.0.1:8019/'; break } }"

cd /d "%POOL_DIR%"
"%PY%" -m uvicorn server:app --host 127.0.0.1 --port 8019

echo.
echo ===== dola-pool exited =====
pause
