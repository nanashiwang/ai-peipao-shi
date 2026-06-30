$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManifestPath = Join-Path $Root "package_manifest.json"

if (!(Test-Path $ManifestPath)) {
    throw "未找到 package_manifest.json，无法校验接入包完整性。"
}

$manifest = Get-Content -Path $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$failed = @()

foreach ($file in $manifest.files) {
    $path = Join-Path $Root $file.path
    if (!(Test-Path $path)) {
        $failed += "缺失文件: $($file.path)"
        continue
    }
    $hash = (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLowerInvariant()
    if ($hash -ne $file.sha256) {
        $failed += "哈希不匹配: $($file.path)"
    }
}

$exePath = Join-Path $Root "wecom_sender.exe"
if (Test-Path $exePath) {
    $signature = Get-AuthenticodeSignature -FilePath $exePath
    if ($signature.Status -ne "Valid") {
        $failed += "EXE 签名无效或未签名: $($signature.Status)"
    }
}

if ($failed.Count -gt 0) {
    $failed | ForEach-Object { Write-Host $_ }
    throw "接入包校验失败，请重新从总控台下载或联系管理员。"
}

Write-Host "接入包校验通过：$($manifest.file_count) 个文件，类型 $($manifest.package_type)。"
