[CmdletBinding()]
param(
    [switch]$SkipWebBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "[NaumiAgent] $Message" -ForegroundColor Cyan
}

function Require-Command {
    param([Parameter(Mandatory)][string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "缺少必需命令：$Name。请先安装后重新运行此脚本。"
    }
    return $command
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$tauriDir = Join-Path $repoRoot "apps\windows\NaumiAgentWorkbench"

Write-Step "检查运行时"
$null = Require-Command "node"
$null = Require-Command "pnpm"
$cargo = Require-Command "cargo"

if (-not $SkipWebBuild) {
    Write-Step "先构建 Web 前端"
    & (Join-Path $PSScriptRoot "build-web.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Web 前端构建失败，退出码：$LASTEXITCODE"
    }
} else {
    Write-Step "跳过 Web 前端构建（-SkipWebBuild）"
}

Write-Step "构建 Tauri 桌面应用 ($tauriDir)"
Set-Location -LiteralPath $tauriDir

# tauri.conf.json 的 beforeBuildCommand 会自动 cd 到 frontend/web 跑 pnpm build，
# 这里通过 -SkipWebBuild 避免重复构建。tauri build 会编译 Rust 并打包。
& $cargo.Source tauri build
if ($LASTEXITCODE -ne 0) {
    throw "Tauri 构建失败，退出码：$LASTEXITCODE"
}

$bundleDir = Join-Path $tauriDir "src-tauri\target\release\bundle"
Write-Step "Tauri 桌面应用构建完成"
if (Test-Path -LiteralPath $bundleDir) {
    Write-Host "  打包产物目录：$bundleDir"
    Get-ChildItem -Path $bundleDir -Recurse -File | ForEach-Object {
        Write-Host "    $($_.FullName)"
    }
} else {
    Write-Host "  可执行文件：$tauriDir\src-tauri\target\release\NaumiAgentWorkbench.exe"
}
