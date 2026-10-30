@echo off
chcp 65001 >nul
title 微信知识助手
cd /d "%~dp0backend"
echo 微信知识助手启动中... 浏览器访问 http://127.0.0.1:8721
python server.py 8721
pause
