@echo off
chcp 65001 >nul
title Qwent - 一键配置
cd /d "%~dp0"
echo 即将执行:依赖安装 → 生成配置 → 下载模型 → 就绪自检
echo (可重复运行,已完成的步骤会自动跳过)
echo.
python backend\scripts\setup.py
echo.
pause
