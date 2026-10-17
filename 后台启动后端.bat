@echo off
chcp 65001 >nul
title 法律助手 - 后端服务（后台模式）
cd /d "%~dp0"
set "KMP_DUPLICATE_LIB_OK=TRUE"
set "PYTHON=D:\an\envs\langchain\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=D:\an\python.exe"

rem 关闭占用 8001 的旧进程（加载新配置必须换进程）
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8001" ^| findstr LISTENING') do (
  echo 停止旧进程 PID %%p
  taskkill /PID %%p /F >nul 2>&1
)
timeout /t 2 /nobreak >nul

rem 用隐藏窗口独立启动：**不依赖任何控制台窗口**，关掉本窗口或其它 cmd 窗口都不会杀掉服务。
rem 日志仍然写到 backend_restart.log；停止服务请运行 stop_backend.bat。
powershell -NoProfile -Command "Start-Process -FilePath '%PYTHON%' -ArgumentList '-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8001' -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardOutput '%~dp0backend_restart.log' -RedirectStandardError '%~dp0backend_restart.err.log'"
echo 后端已在后台启动（无窗口）。页面: http://127.0.0.1:8001/ui/
echo 日志: backend_restart.log    停止: stop_backend.bat
timeout /t 5 /nobreak >nul
netstat -ano | findstr ":8001" | findstr LISTENING >nul && echo [OK] 8001 已监听 || echo [失败] 8001 未监听，请查看 backend_restart.err.log
