param(
    [string]$PythonExe = "python",
    [string]$OutputDir = "dist/rpa-client-exe",
    [string]$CertThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$SkipSign
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$BuildVenv = Join-Path $RepoRoot ".venv-build-rpa"
$BuildPython = Join-Path $BuildVenv "Scripts/python.exe"
$OutputPath = Join-Path $RepoRoot $OutputDir
$ExePath = Join-Path $RepoRoot "dist/wecom_sender.exe"
$TargetExe = Join-Path $OutputPath "wecom_sender.exe"

if (!(Test-Path $BuildPython)) {
    & $PythonExe -m venv $BuildVenv
}

& $BuildPython -m pip install --upgrade pip -q
& $BuildPython -m pip install -r "rpa/requirements-client.txt" pyinstaller

& $BuildPython -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --name wecom_sender `
    --paths "." `
    --hidden-import "app.services.ark_client" `
    "rpa/wecom_sender.py"

if (!(Test-Path $ExePath)) {
    throw "PyInstaller 未生成 dist/wecom_sender.exe"
}

Remove-Item -Recurse -Force $OutputPath -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $OutputPath "rpa") | Out-Null
Copy-Item -Path $ExePath -Destination $TargetExe
Copy-Item -Path "rpa/config.example.json" -Destination (Join-Path $OutputPath "rpa/config.json")
Copy-Item -Path "rpa/templates/watchdog_exe.ps1" -Destination (Join-Path $OutputPath "watchdog_exe.ps1")
Copy-Item -Path "rpa/templates/启动EXE.bat" -Destination (Join-Path $OutputPath "启动EXE.bat")
Copy-Item -Path "rpa/templates/校验接入包.ps1" -Destination (Join-Path $OutputPath "校验接入包.ps1")
Copy-Item -Path "rpa/templates/install_autostart.bat" -Destination (Join-Path $OutputPath "install_autostart.bat")
Copy-Item -Path "rpa/templates/uninstall_autostart.bat" -Destination (Join-Path $OutputPath "uninstall_autostart.bat")
Copy-Item -Path "rpa/templates/使用说明.txt" -Destination (Join-Path $OutputPath "使用说明.txt")

$SignatureStatus = "unsigned"
if (!$SkipSign -and $CertThumbprint) {
    $signtool = (Get-Command signtool.exe -ErrorAction SilentlyContinue)
    if (!$signtool) {
        throw "未找到 signtool.exe。请安装 Windows SDK，或使用 -SkipSign 生成未签名包。"
    }
    & $signtool.Source sign /fd SHA256 /sha1 $CertThumbprint /tr $TimestampUrl /td SHA256 $TargetExe
    $signature = Get-AuthenticodeSignature -FilePath $TargetExe
    if ($signature.Status -ne "Valid") {
        throw "EXE 签名失败：$($signature.Status)"
    }
    $SignatureStatus = "signed"
}

& $BuildPython -m rpa.build_package_manifest `
    --root $OutputPath `
    --package-type "rpa-client-exe" `
    --signature-status $SignatureStatus

Compress-Archive -Path (Join-Path $OutputPath "*") -DestinationPath (Join-Path $RepoRoot "dist/rpa-client-exe.zip") -Force
Write-Host "EXE 接入包已生成：$OutputPath"
Write-Host "ZIP：dist/rpa-client-exe.zip"
