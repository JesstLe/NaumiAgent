$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Info([string]$Message) { Write-Host "[naumi] $Message" -ForegroundColor Cyan }

$ReleaseRepo = if ($env:NAUMI_RELEASE_REPO) { $env:NAUMI_RELEASE_REPO } else { "JesstLe/NaumiAgent-Releases" }
$Version = if ($env:NAUMI_VERSION) { $env:NAUMI_VERSION } else { "latest" }
$InstallRoot = if ($env:NAUMI_INSTALL_ROOT) { $env:NAUMI_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA "NaumiAgent" }
$BinDir = if ($env:NAUMI_BIN_DIR) { $env:NAUMI_BIN_DIR } else { Join-Path $InstallRoot "bin" }

$Arch = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()) {
    "X64" { "x64" }
    "Arm64" { "arm64" }
    default { throw "不支持的处理器架构：$_" }
}

if ($env:NAUMI_RELEASE_BASE_URL) {
    $BaseUrl = $env:NAUMI_RELEASE_BASE_URL.TrimEnd('/')
} elseif ($Version -eq "latest") {
    $BaseUrl = "https://github.com/$ReleaseRepo/releases/latest/download"
} else {
    if ($Version -notmatch '^[A-Za-z0-9._-]+$') { throw "NAUMI_VERSION 含不安全字符。" }
    $BaseUrl = "https://github.com/$ReleaseRepo/releases/download/v$Version"
}
$Asset = if ($Version -eq "latest") { "naumi-windows-$Arch.zip" } else { "naumi-$Version-windows-$Arch.zip" }

$Temp = Join-Path ([System.IO.Path]::GetTempPath()) ("naumi-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Temp | Out-Null
try {
    $Archive = Join-Path $Temp $Asset
    $Checksum = "$Archive.sha256"
    Write-Info "下载 $Asset"
    Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/$Asset" -OutFile $Archive
    Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/$Asset.sha256" -OutFile $Checksum
    $Expected = ((Get-Content -LiteralPath $Checksum -TotalCount 1) -split '\s+')[0]
    if ($Expected -notmatch '^[0-9A-Fa-f]{64}$') { throw "checksum 文件格式无效。" }
    $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash
    if ($Actual -ne $Expected) { throw "SHA-256 校验失败，已拒绝安装。" }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($Entry in $Zip.Entries) {
            $Name = $Entry.FullName.Replace('\', '/')
            $Segments = @($Name -split '/')
            $ExpectedRoot = "^naumi-.+-windows-$([regex]::Escape($Arch))(/|$)"
            if ([System.IO.Path]::IsPathRooted($Name) -or $Segments -contains "..") {
                throw "安装包含不安全路径：$Name"
            }
            if ($Name -notmatch $ExpectedRoot) {
                throw "安装包含契约外路径：$Name"
            }
        }
    } finally {
        $Zip.Dispose()
    }

    $Extract = Join-Path $Temp "extract"
    Expand-Archive -LiteralPath $Archive -DestinationPath $Extract
    $Bundles = @(Get-ChildItem -LiteralPath $Extract -Directory | Where-Object { $_.Name -match "^naumi-.+-windows-$Arch$" })
    if ($Bundles.Count -ne 1) { throw "安装包顶层目录不符合发行契约。" }
    $Bundle = $Bundles[0]
    foreach ($Required in @("manifest.json", "naumi.exe", "naumi-ui.exe")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Bundle.FullName $Required) -PathType Leaf)) {
            throw "安装包缺少 $Required。"
        }
    }

    $Releases = Join-Path $InstallRoot "releases"
    New-Item -ItemType Directory -Force -Path $Releases, $BinDir | Out-Null
    $Destination = Join-Path $Releases $Bundle.Name
    if (Test-Path -LiteralPath $Destination) { throw "该版本已安装：$Destination" }
    $Staged = Join-Path $InstallRoot (".install-" + $Bundle.Name + "-" + $PID)
    Move-Item -LiteralPath $Bundle.FullName -Destination $Staged
    Move-Item -LiteralPath $Staged -Destination $Destination

    $ShimTemp = Join-Path $BinDir "naumi.cmd.new"
    $Shim = Join-Path $BinDir "naumi.cmd"
    Set-Content -LiteralPath $ShimTemp -Encoding ASCII -Value "@`"$Destination\naumi.exe`" %*"
    Move-Item -Force -LiteralPath $ShimTemp -Destination $Shim

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $PathEntries = @($UserPath -split ';' | Where-Object { $_ })
    if ($PathEntries -notcontains $BinDir) {
        [Environment]::SetEnvironmentVariable("Path", (($PathEntries + $BinDir) -join ';'), "User")
        Write-Warning "$BinDir 已加入用户 PATH；请重新打开终端。"
    }
    Write-Info "安装完成：$Destination"
    Write-Info "运行：naumi"
} finally {
    Remove-Item -Recurse -Force $Temp -ErrorAction SilentlyContinue
}
