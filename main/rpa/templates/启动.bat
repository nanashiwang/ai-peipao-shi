@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo ============================================
echo   AI Coach Remote Client
echo ============================================
if not exist .venv\Scripts\python.exe (
    echo First run: creating environment and installing dependencies, please wait 1-2 minutes...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip -q
    .venv\Scripts\python.exe -m pip install -r requirements-client.txt
)
echo Client running. Keep WeCom in foreground. Close this window to stop.
:loop
.venv\Scripts\python.exe rpa\wecom_sender.py --config rpa\config.json
timeout /t 15 /nobreak >nul
goto loop
