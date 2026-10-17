@echo off
chcp 65001 >nul
title 法律助手 - 后端服务（重启脚本）
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
timeout /t 3 /nobreak >nul

echo 启动后端（日志：backend_restart.log）
"%PYTHON%" -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001 > backend_restart.log 2>&1
