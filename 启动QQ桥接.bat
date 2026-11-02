@echo off
setlocal
cd /d "%~dp0"

echo [qqbot] starting local WeChat bridge on 127.0.0.1:18821 ...
start "wechat-qqbot-bridge" /min python backend\qqbot_bridge.py

echo [qqbot] starting reverse tunnel to tencent-qqbot ...
:loop
ssh -N -R 127.0.0.1:18821:127.0.0.1:18821 ^
  -o ExitOnForwardFailure=yes ^
  -o ServerAliveInterval=30 ^
  -o ServerAliveCountMax=3 ^
  tencent-qqbot
echo [qqbot] tunnel exited, retrying in 5s ...
timeout /t 5 /nobreak >nul
goto loop
