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
$tauriDir = Join-Path $repoRoot "apps\windows\NaumiAgentWorkbench"

Write-Step "检查运行时"
$null = Require-Command "node"
$null = Require-Command "pnpm"
$cargo = Require-Command "cargo"

Write-Step "启动开发模式（Vite + Tauri）"
Write-Host "  前端：$repoRoot\frontend\web"
Write-Host "  Tauri：$tauriDir"
Write-Host "  tauri.conf.json 的 beforeDevCommand 会自动启动 Vite dev server。"
Write-Host ""

Set-Location -LiteralPath $tauriDir
& $cargo.Source tauri dev
if ($LASTEXITCODE -ne 0) {
    throw "Tauri 开发模式异常退出，退出码：$LASTEXITCODE"
}
