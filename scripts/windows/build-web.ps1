[CmdletBinding()]
param()

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
$webDir = Join-Path $repoRoot "frontend\web"

Write-Step "检查运行时"
$null = Require-Command "node"
$pnpm = Require-Command "pnpm"

Write-Step "构建 Web 前端 ($webDir)"
Set-Location -LiteralPath $webDir

& $pnpm.Source install
if ($LASTEXITCODE -ne 0) {
    throw "pnpm install 失败，退出码：$LASTEXITCODE"
}

& $pnpm.Source run build
if ($LASTEXITCODE -ne 0) {
    throw "Web 前端构建失败，退出码：$LASTEXITCODE"
}

$distDir = Join-Path $webDir "dist"
if (-not (Test-Path -LiteralPath $distDir)) {
    throw "构建产物目录未生成：$distDir"
}

Write-Step "Web 前端构建完成"
Write-Host "  产物目录：$distDir"
