@echo off
chcp 65001 >nul
title 法律助手 - 后端服务
cd /d "%~dp0"
set "KMP_DUPLICATE_LIB_OK=TRUE"
echo ============================================================
echo   前端页面 : http://127.0.0.1:8001/ui/
echo   接口文档 : http://127.0.0.1:8001/docs
echo   按 Ctrl+C 停止服务（关闭本窗口也会停止）
echo ============================================================
start "" http://127.0.0.1:8001/ui/
set "PYTHON=D:\an\envs\langchain\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=D:\an\python.exe"
if not exist "%PYTHON%" (
  echo [错误] 找不到 Python。请安装 Python 或修改 启动.bat 中的 PYTHON 路径。
  pause
  exit /b 1
)
echo   解释器   : %PYTHON%
"%PYTHON%" -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001
pause
