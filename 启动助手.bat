@echo off
chcp 65001 >nul
title 微信知识助手
cd /d "%~dp0"
if not exist "backend\config.json" (
    echo [!] 尚未完成初始配置,请先双击运行 一键配置.bat
    pause
    exit /b 1
)
echo 微信知识助手启动中... 浏览器访问 http://127.0.0.1:8721
cd backend
python server.py 8721
pause
