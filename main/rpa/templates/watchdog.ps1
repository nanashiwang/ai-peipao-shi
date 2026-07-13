$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "rpa-watchdog.log"
$StopFile = Join-Path $Root "STOP_RPA"

function Write-WatchdogLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Ensure-PythonEnv {
    $PythonExe = Join-Path $Root ".venv/Scripts/python.exe"
    if (!(Test-Path $PythonExe)) {
        Write-WatchdogLog "creating virtual environment"
        python -m venv .venv
        & $PythonExe -m pip install --upgrade pip -q
        & $PythonExe -m pip install -r requirements-client.txt
    }
    return $PythonExe
}

Write-WatchdogLog "watchdog started. create STOP_RPA file to stop gracefully."
$PythonExe = Ensure-PythonEnv
$RestartDelaySeconds = 15

while (!(Test-Path $StopFile)) {
    Write-WatchdogLog "starting wecom_sender.py"
    $code = 1
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Python writes normal diagnostics to stderr; do not let PowerShell stop the watchdog.
        $ErrorActionPreference = "Continue"
        & $PythonExe "rpa/wecom_sender.py" --config "rpa/config.json" 2>&1 | ForEach-Object {
            Add-Content -Path $LogFile -Value $_ -Encoding UTF8
            Write-Host $_
        }
        $code = $LASTEXITCODE
    }
    catch {
        Write-WatchdogLog "wecom_sender failed: $($_.Exception.Message)"
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if (Test-Path $StopFile) {
        break
    }
    Write-WatchdogLog "wecom_sender exited with code=$code, restarting in ${RestartDelaySeconds}s"
    Start-Sleep -Seconds $RestartDelaySeconds
}

Write-WatchdogLog "watchdog stopped"
