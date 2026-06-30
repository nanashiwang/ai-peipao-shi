@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Installing AI Coach RPA autostart task...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$root=(Get-Location).Path; $action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File \"' + (Join-Path $root 'watchdog.ps1') + '\"'); $trigger=New-ScheduledTaskTrigger -AtLogOn; $settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); Register-ScheduledTask -TaskName 'AI-Coach-RPA-Client' -Action $action -Trigger $trigger -Settings $settings -Description 'AI Coach RPA remote client watchdog' -Force | Out-Null"
if errorlevel 1 (
  echo Install failed. Please run this file again, or contact the administrator.
  pause
  exit /b 1
)
echo Installed. It will start after this Windows user logs in.
pause
