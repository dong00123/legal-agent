@echo off
chcp 65001 >nul
title 法律助手 - 停止后端
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8001" ^| findstr LISTENING') do (
  echo 停止后端 PID %%p
  taskkill /PID %%p /F >nul 2>&1
)
echo 完成。
pause
