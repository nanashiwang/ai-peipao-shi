@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo ============================================
echo   AI Coach Remote Client
echo ============================================
if exist watchdog.ps1 (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog.ps1"
    exit /b %ERRORLEVEL%
)
echo watchdog.ps1 not found, fallback to simple loop.
if not exist .venv\Scripts\python.exe (
    echo First run: creating environment and installing dependencies, please wait 1-2 minutes...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip -q
    .venv\Scripts\python.exe -m pip install -r requirements-client.txt
)
:loop
.venv\Scripts\python.exe rpa\wecom_sender.py --config rpa\config.json
timeout /t 15 /nobreak >nul
goto loop
