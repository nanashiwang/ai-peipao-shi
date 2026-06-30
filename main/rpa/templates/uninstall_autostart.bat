@echo off
chcp 65001 >nul
echo Removing AI Coach RPA autostart task...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Unregister-ScheduledTask -TaskName 'AI-Coach-RPA-Client' -Confirm:$false -ErrorAction SilentlyContinue"
if errorlevel 1 (
  echo Remove failed. Please contact the administrator.
  pause
  exit /b 1
)
echo Removed. To stop a currently running client, close its window or create a STOP_RPA file in this folder.
pause
