@echo off
rem ============================================================
rem 后端自启动入口（计划任务 LegalBackend / 登录时触发）
rem - 自包含：不引用中文文件名的 bat（cmd 按 GBK 解析 UTF-8 中文路径会乱码失败）
rem - 幂等：8001 上已有健康实例时直接跳过，不重复起进程
rem - 停止服务用 stop_backend.bat；日志见 backend_restart.log / .err.log
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHON=D:\an\envs\langchain\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=D:\an\python.exe"
set "KMP_DUPLICATE_LIB_OK=TRUE"

rem 已有健康实例就不动它（保护手动启动的进程）
powershell -NoProfile -Command "try{Invoke-WebRequest -Uri 'http://127.0.0.1:8001/api/v1/health' -UseBasicParsing -TimeoutSec 3|Out-Null;exit 0}catch{exit 1}"
if %errorlevel%==0 (
  echo [SKIP] 后端已在运行，无需重启
  exit /b 0
)

rem 残留的半死进程（占着端口但健康检查不过）才清掉
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8001" ^| findstr LISTENING') do taskkill /PID %%p /F >nul 2>&1

rem 隐藏窗口独立启动：不依赖任何控制台窗口，关窗口不会杀掉服务
powershell -NoProfile -Command "Start-Process -FilePath '%PYTHON%' -ArgumentList '-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8001' -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardOutput '%~dp0backend_restart.log' -RedirectStandardError '%~dp0backend_restart.err.log'"

rem timeout 在计划任务的非交互会话里会报错，用 ping 计时；模型加载要 1~2 分钟，轮询等待
for /l %%i in (1,1,24) do (
  netstat -ano | findstr ":8001" | findstr LISTENING >nul && exit /b 0
  ping -n 6 127.0.0.1 >nul
)
exit /b 1
