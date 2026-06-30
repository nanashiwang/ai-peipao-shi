$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$ExePath = Join-Path $Root "wecom_sender.exe"
$ConfigPath = Join-Path $Root "rpa/config.json"
$LogDir = Join-Path $Root "logs"
$StopFile = Join-Path $Root "STOP_RPA"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "rpa-watchdog.log"

function Write-WatchdogLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

if (!(Test-Path $ExePath)) {
    throw "未找到 wecom_sender.exe，请确认这是 EXE 版接入包。"
}
if (!(Test-Path $ConfigPath)) {
    throw "未找到 rpa/config.json，请重新从总控台下载设备接入包。"
}

Write-WatchdogLog "exe watchdog started. create STOP_RPA file to stop gracefully."
$RestartDelaySeconds = 15

while (!(Test-Path $StopFile)) {
    Write-WatchdogLog "starting wecom_sender.exe"
    & $ExePath --config $ConfigPath 2>&1 | ForEach-Object {
        Add-Content -Path $LogFile -Value $_ -Encoding UTF8
        Write-Host $_
    }
    $code = $LASTEXITCODE
    if (Test-Path $StopFile) {
        break
    }
    Write-WatchdogLog "wecom_sender.exe exited with code=$code, restarting in ${RestartDelaySeconds}s"
    Start-Sleep -Seconds $RestartDelaySeconds
}

Write-WatchdogLog "exe watchdog stopped"
