@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] 准备 MSVC 构建环境...
set "VCVARS="
if exist "%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" (
    for /f "usebackq tokens=*" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do (
        if exist "%%i\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%%i\VC\Auxiliary\Build\vcvars64.bat"
    )
)
if not defined VCVARS if exist "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if not defined VCVARS (
    echo [!] 未找到 vcvars64.bat，请安装 VS 2022 BuildTools（含 C++ 生成工具）
    exit /b 1
)
call "%VCVARS%" >nul
if errorlevel 1 (
    echo [!] 初始化 MSVC 环境失败
    exit /b 1
)

echo [2/3] 安装 Tauri CLI...
if not exist "node_modules" (
    call npm install --no-audit --no-fund
    if errorlevel 1 (
        echo [!] 默认源失败，改用 npmmirror...
        call npm install --no-audit --no-fund --registry=https://registry.npmmirror.com
        if errorlevel 1 (
            echo [!] npm install 失败
            exit /b 1
        )
    )
)

echo [3/3] 编译桌面壳（首次约 10-30 分钟）...
call npx tauri build --no-bundle
exit /b %errorlevel%
