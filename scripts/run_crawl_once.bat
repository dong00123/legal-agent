@echo off
REM Run ONE crawl round (statutes + cases). Called by Windows Task Scheduler.
REM ASCII-only on purpose: cmd's default code page may not render UTF-8 comments.
REM Logs are appended to logs\crawl.log; the exit code is the runner's exit code
REM (0 = ok/skipped, 3 = paused/needs human, 1 = failed).
setlocal
REM Switch the console to UTF-8 BEFORE any echo, so the %date%/%time% lines are
REM written in the same encoding as Python's output (otherwise the log mixes
REM GBK timestamps with UTF-8 payload and looks garbled in any editor).
chcp 65001 >nul

set "REPO=%~dp0.."
cd /d "%REPO%" || exit /b 1
if not exist "logs" mkdir "logs"

REM Force UTF-8 so the redirected log file has readable Chinese (console code page
REM on Windows defaults to GBK and would mangle the log).
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY=%REPO%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [%date% %time%] ==== crawl round start ==== >> "logs\crawl.log"
"%PY%" -m backend.crawler.runner --once --json >> "logs\crawl.log" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] ==== exit %RC% ==== >> "logs\crawl.log"
endlocal & exit /b %RC%
