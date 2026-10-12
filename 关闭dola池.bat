@echo off
setlocal

rem ============================================
rem  Close dola pool service (uvicorn)
rem  Usage:
rem    关闭dola池.bat          -> kill process listening on port 8002 (default)
rem    关闭dola池.bat 8003     -> kill process listening on port 8003
rem    关闭dola池.bat python   -> kill all python.exe processes
rem ============================================

set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=8002"

rem --- If target is a number, treat it as a port ---
echo %TARGET%| findstr /r "^[0-9][0-9]*$" >nul
if %errorlevel%==0 (
    call :kill_by_port %TARGET%
) else (
    call :kill_by_name %TARGET%
)

echo.
pause
endlocal
exit /b 0

:kill_by_port
set "PORT=%~1"
rem Use Get-NetTCPConnection for exact port match (never wildcards).
rem Guard: never touch PID 0 (Idle) or PID 4 (System).
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$pids = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique;" ^
    "if (-not $pids) { Write-Host '[DOLA] No process listening on port %PORT%. Nothing to kill.' }" ^
    "foreach ($p in $pids) {" ^
    "  if ($p -eq 0 -or $p -eq 4) { continue }" ^
    "  Write-Host ('[DOLA] Found PID ' + $p + ' listening on port %PORT%, killing...');" ^
    "  taskkill /F /T /PID $p 2>$null;" ^
    "  if ($LASTEXITCODE -eq 0) { Write-Host ('[DOLA] PID ' + $p + ' terminated.') } else { Write-Host '[DOLA] WARN: failed to kill PID ' + $p + ', try running as Administrator.' }" ^
    "}"
exit /b 0

:kill_by_name
set "NAME=%~1"
echo [DOLA] Killing all processes named "%NAME%"...
taskkill /F /IM "%NAME%" >nul 2>&1
if %errorlevel%==0 (
    echo [DOLA] Done.
) else (
    echo [DOLA] No process named "%NAME%" found.
)
exit /b 0
