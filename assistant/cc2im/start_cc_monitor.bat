@echo off
rem =====================================================================
rem  cc-monitor service launcher (Claude Code -> Feishu)
rem  Usage: double-click, or run from anywhere. Starts cc2im.cc_service
rem  (from the assistant/ directory, where the cc2im package lives).
rem =====================================================================
setlocal
cd /d "%~dp0.."

set "PYTHON=D:\anaconda3\envs\main\python.exe"
if not exist "%PYTHON%" (
  echo [cc-monitor] python not found: %PYTHON%
  pause
  exit /b 1
)

rem ---- Port already in use -> service may already be running ----
netstat -ano | findstr ":8790" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [cc-monitor] Port 8790 is in use - the service may already be running.
  echo [cc-monitor] Health check:
  echo.
  curl -s http://127.0.0.1:8790/health
  echo.
  echo.
  echo [cc-monitor] If you want to restart it, close this window and
  echo [cc-monitor] kill the python process first, then run this again.
  pause
  exit /b 1
)

echo [cc-monitor] Starting service. Log -> local\_cc_service.log
echo [cc-monitor] Press Ctrl+C to stop.
"%PYTHON%" -u -m cc2im.cc_service > "local\_cc_service.log" 2>&1
