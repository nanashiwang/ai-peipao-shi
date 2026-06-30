@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo ============================================
echo   AI Coach Remote Client EXE
echo ============================================
if exist "校验接入包.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0校验接入包.ps1"
    if errorlevel 1 exit /b %ERRORLEVEL%
)
if exist watchdog_exe.ps1 (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog_exe.ps1"
    exit /b %ERRORLEVEL%
)
echo watchdog_exe.ps1 not found, fallback to simple loop.
if not exist wecom_sender.exe (
    echo wecom_sender.exe not found.
    exit /b 1
)
:loop
wecom_sender.exe --config rpa\config.json
timeout /t 15 /nobreak >nul
goto loop
