@echo off
chcp 65001 >nul
title 构建Qwent桌面版
cd /d "%~dp0"

call "desktop\build.bat"
if errorlevel 1 (
    echo.
    echo [!] 构建失败，请查看上方输出
    pause
    exit /b 1
)

copy /Y "desktop\src-tauri\target\release\wechat-assistant.exe" "Qwent.exe" >nul
echo.
echo 构建完成：%~dp0Qwent.exe
echo 双击即可启动；关闭窗口会留在系统托盘。
pause
