$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$captureDir = Join-Path $projectRoot "docs\showcase\screenshots"
$chromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$profileDir = Join-Path ([System.IO.Path]::GetTempPath()) ("naumi-showcase-" + [guid]::NewGuid().ToString("N"))

if (-not (Test-Path -LiteralPath $chromePath)) {
    throw "未找到 Chrome: $chromePath"
}

$htmlFiles = Get-ChildItem -LiteralPath $captureDir -Filter "*.html" |
    Where-Object { $_.Name -ne "00-overview.html" } |
    Sort-Object Name
if ($htmlFiles.Count -eq 0) {
    throw "没有找到待截图的 HTML 文件。"
}

New-Item -ItemType Directory -Path $profileDir | Out-Null
try {
    foreach ($htmlFile in $htmlFiles) {
        $pngPath = [System.IO.Path]::ChangeExtension($htmlFile.FullName, ".png")
        $fileUrl = ([System.Uri]$htmlFile.FullName).AbsoluteUri
        & $chromePath `
            --headless=new `
            --disable-gpu `
            --hide-scrollbars `
            --no-first-run `
            "--user-data-dir=$profileDir" `
            --force-device-scale-factor=1 `
            --window-size=1440,900 `
            "--screenshot=$pngPath" `
            $fileUrl | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pngPath)) {
            throw "截图失败: $($htmlFile.Name)"
        }
    }

    $overviewHtml = Join-Path $captureDir "00-overview.html"
    if (Test-Path -LiteralPath $overviewHtml) {
        $overviewPng = Join-Path $captureDir "00-overview.png"
        $overviewUrl = ([System.Uri]$overviewHtml).AbsoluteUri
        & $chromePath `
            --headless=new `
            --disable-gpu `
            --hide-scrollbars `
            --no-first-run `
            "--user-data-dir=$profileDir" `
            --force-device-scale-factor=1 `
            --window-size=1440,1100 `
            "--screenshot=$overviewPng" `
            $overviewUrl | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $overviewPng)) {
            throw "总览截图失败。"
        }
    }
} finally {
    Remove-Item -LiteralPath $profileDir -Recurse -Force -ErrorAction SilentlyContinue
}

Get-ChildItem -LiteralPath $captureDir -Filter "*.png" |
    Sort-Object Name |
    Select-Object Name, Length, LastWriteTime
