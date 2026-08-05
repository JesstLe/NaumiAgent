$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Info([string]$Message) { Write-Host "[naumi] $Message" -ForegroundColor Cyan }

function Get-DirectoryFingerprint([string]$Root) {
    $Resolved = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    return @(
        Get-ChildItem -LiteralPath $Resolved -Recurse -File | ForEach-Object {
            $Relative = $_.FullName.Substring($Resolved.Length).TrimStart('\').Replace('\', '/')
            "$Relative $((Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash)"
        } | Sort-Object
    ) -join "`n"
}

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
    foreach ($Required in @("manifest.json", "launcher\naumi.exe", "naumi-runtime.exe", "naumi-ui.exe")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Bundle.FullName $Required) -PathType Leaf)) {
            throw "安装包缺少 $Required。"
        }
    }

    $LaunchersDir = Join-Path $InstallRoot "launchers"
    $LauncherDestination = Join-Path $LaunchersDir $Bundle.Name
    $Launcher = Join-Path $LauncherDestination "naumi.exe"
    New-Item -ItemType Directory -Force -Path $LaunchersDir, $BinDir | Out-Null
    if (Test-Path -LiteralPath $LauncherDestination) {
        $ExpectedLauncher = Get-DirectoryFingerprint (Join-Path $Bundle.FullName "launcher")
        $InstalledLauncher = Get-DirectoryFingerprint $LauncherDestination
        if ($ExpectedLauncher -ne $InstalledLauncher) {
            throw "同版本 Launcher 内容冲突，拒绝覆盖：$LauncherDestination"
        }
    } else {
        $LauncherStaged = Join-Path $LaunchersDir (".install-" + $Bundle.Name + "-" + $PID)
        Remove-Item -Recurse -Force $LauncherStaged -ErrorAction SilentlyContinue
        Copy-Item -Recurse -LiteralPath (Join-Path $Bundle.FullName "launcher") -Destination $LauncherStaged
        Move-Item -LiteralPath $LauncherStaged -Destination $LauncherDestination
        Get-ChildItem -LiteralPath $LauncherDestination -Recurse -File | ForEach-Object {
            $_.IsReadOnly = $true
        }
    }
    & $Launcher --launcher-self-test | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "稳定 Launcher 自检失败，拒绝安装。" }
    $env:NAUMI_INSTALL_ROOT = $InstallRoot
    & $Launcher --launcher-install $Bundle.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "版本槽安装或激活失败，PATH 仍指向上一稳定 Launcher。"
    }

    $ShimTemp = Join-Path $BinDir "naumi.cmd.new"
    $Shim = Join-Path $BinDir "naumi.cmd"
    Set-Content -LiteralPath $ShimTemp -Encoding ASCII -Value "@`"$Launcher`" %*"
    Move-Item -Force -LiteralPath $ShimTemp -Destination $Shim

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $PathEntries = @($UserPath -split ';' | Where-Object { $_ })
    if ($PathEntries -notcontains $BinDir) {
        [Environment]::SetEnvironmentVariable("Path", (($PathEntries + $BinDir) -join ';'), "User")
        Write-Warning "$BinDir 已加入用户 PATH；请重新打开终端。"
    }
    Write-Info "安装完成：active version slot 已原子切换。"
    Write-Info "运行：naumi"
} finally {
    Remove-Item -Recurse -Force $Temp -ErrorAction SilentlyContinue
}
