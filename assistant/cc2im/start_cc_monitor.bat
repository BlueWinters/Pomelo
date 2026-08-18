@echo off
setlocal enabledelayedexpansion
rem =====================================================================
rem  cc-monitor service launcher (Claude Code -> Feishu) - Windows
rem
rem  Usage (double-click = foreground; or run with an argument):
rem    start_cc_monitor.bat             Run in foreground (Ctrl+C to stop)
rem    start_cc_monitor.bat bg          Run in background (minimized, logs to file)
rem    start_cc_monitor.bat stop        Stop the service (kills process on port 8790)
rem    start_cc_monitor.bat restart     Restart (stop, then run foreground)
rem    start_cc_monitor.bat status      Show status + health check
rem
rem  Python lookup order:
rem    1) env var CC_MONITOR_PYTHON (full path to python.exe)
rem    2) common anaconda / miniconda paths
rem    3) python / python3 from PATH (skips the broken Store stub)
rem
rem  NOTE: keep this file ASCII-only. cmd on Chinese-locale Windows
rem  (codepage 936) mis-parses UTF-8 multibyte text in .bat files.
rem =====================================================================

rem ---- Enter assistant/ (where the cc2im package lives) ----
cd /d "%~dp0.."
if not exist "local" mkdir "local"

rem ---- Locate Python ----
set "PYTHON="
if defined CC_MONITOR_PYTHON if exist "%CC_MONITOR_PYTHON%" set "PYTHON=%CC_MONITOR_PYTHON%"
if not defined PYTHON if exist "D:\anaconda3\envs\main\python.exe" set "PYTHON=D:\anaconda3\envs\main\python.exe"
if not defined PYTHON if exist "%USERPROFILE%\anaconda3\envs\main\python.exe" set "PYTHON=%USERPROFILE%\anaconda3\envs\main\python.exe"
if not defined PYTHON if exist "%USERPROFILE%\miniconda3\envs\main\python.exe" set "PYTHON=%USERPROFILE%\miniconda3\envs\main\python.exe"
if not defined PYTHON call :find_in_path
if not defined PYTHON (
    echo [cc-monitor] ERROR: Python interpreter not found.
    echo [cc-monitor] Set env var CC_MONITOR_PYTHON to the full path of python.exe and retry.
    pause
    exit /b 1
)
"%PYTHON%" -c "pass" >nul 2>&1
if errorlevel 1 (
    echo [cc-monitor] ERROR: found Python is not usable: %PYTHON%
    pause
    exit /b 1
)
echo [cc-monitor] Using Python: %PYTHON%

rem ---- Dispatch subcommands ----
if /i "%1"=="stop"    goto :stop
if /i "%1"=="status"  goto :status
if /i "%1"=="bg"      goto :background
if /i "%1"=="restart" goto :restart
goto :foreground

rem =====================================================================
rem  Foreground run
rem =====================================================================
:foreground
call :port_in_use
if not errorlevel 1 goto :already_running
call :check_deps
if errorlevel 1 exit /b 1
echo [cc-monitor] Starting service in foreground (Ctrl+C to stop). Log also written to local\_cc_service.log
"%PYTHON%" -u -m cc2im.cc_service 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath 'local\_cc_service.log'"
echo.
echo [cc-monitor] Service stopped.
exit /b 0

rem =====================================================================
rem  Background run (minimized window, logs to file)
rem =====================================================================
:background
call :port_in_use
if not errorlevel 1 goto :already_running
call :check_deps
if errorlevel 1 exit /b 1
echo [cc-monitor] Starting service in background (minimized). Log: local\_cc_service.log
start "cc-monitor" /min powershell -NoProfile -WindowStyle Hidden -Command "& '%PYTHON%' -u -m cc2im.cc_service *> '%CD%\local\_cc_service.log'"
echo [cc-monitor] Started. Use "status" to check, "stop" to stop.
exit /b 0

rem =====================================================================
rem  Stop service (kill the process listening on 8790)
rem =====================================================================
:stop
set "PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8790" ^| findstr "LISTENING"') do set "PID=%%p"
if not defined PID (
    echo [cc-monitor] No process listening on 8790 - service is probably not running.
    exit /b 0
)
echo [cc-monitor] Killing PID=!PID! (listening on 8790) ...
taskkill /PID !PID! /F >nul 2>&1
if errorlevel 1 (
    echo [cc-monitor] Failed to kill. Close the process manually.
) else (
    echo [cc-monitor] Stopped.
)
exit /b 0

rem =====================================================================
rem  Restart (stop, then foreground)
rem =====================================================================
:restart
call :stop
goto :foreground

rem =====================================================================
rem  Status + health check
rem =====================================================================
:status
call :port_in_use
if errorlevel 1 (
    echo [cc-monitor] Service is not running - nothing on port 8790.
    exit /b 0
)
echo [cc-monitor] Service is running. Health check:
curl -s http://127.0.0.1:8790/health
echo.
exit /b 0

rem =====================================================================
rem  Subroutines
rem =====================================================================
:port_in_use
netstat -ano | findstr ":8790" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0

:find_in_path
for %%E in (python.exe python3.exe) do (
    for /f "delims=" %%p in ('where %%E 2^>nul') do (
        rem Skip the Windows Store stub - it pops up an install prompt and is useless
        echo %%p | findstr /i "WindowsApps" >nul 2>&1
        if errorlevel 1 (
            "%%p" -c "pass" >nul 2>&1
            if not errorlevel 1 (
                set "PYTHON=%%p"
                exit /b 0
            )
        )
    )
)
exit /b 0

:check_deps
"%PYTHON%" -c "import fastapi, uvicorn, loguru" >nul 2>&1
if errorlevel 1 (
    echo [cc-monitor] ERROR: missing dependencies fastapi / uvicorn / loguru.
    echo [cc-monitor] Install them first:
    echo [cc-monitor]   "%PYTHON%" -m pip install fastapi uvicorn loguru
    exit /b 1
)
"%PYTHON%" -c "import lark_oapi" >nul 2>&1
if not errorlevel 1 exit /b 0
echo [cc-monitor] WARN: lark-oapi not installed - Feishu WebSocket gateway (card button callbacks) unavailable.
echo [cc-monitor] Install it with: "%PYTHON%" -m pip install lark-oapi
exit /b 0

:already_running
echo [cc-monitor] Port 8790 is in use - the service may already be running. Health check:
curl -s http://127.0.0.1:8790/health
echo.
echo [cc-monitor] To restart: run "stop" first, then "start".
pause
exit /b 1
