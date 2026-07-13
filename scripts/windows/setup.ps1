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

function Resolve-GitBash {
    $candidates = [System.Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($env:NAUMI_GIT_BASH)) {
        $candidates.Add($env:NAUMI_GIT_BASH)
    }

    $git = Require-Command "git"
    $gitRoot = Split-Path -Parent (Split-Path -Parent $git.Source)
    $candidates.Add((Join-Path $gitRoot "bin\bash.exe"))

    if ($env:ProgramFiles) {
        $candidates.Add((Join-Path $env:ProgramFiles "Git\bin\bash.exe"))
    }
    $programFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if ($programFilesX86) {
        $candidates.Add((Join-Path $programFilesX86 "Git\bin\bash.exe"))
    }
    if ($env:LOCALAPPDATA) {
        $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Git\bin\bash.exe"))
    }

    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $resolved = (Resolve-Path -LiteralPath $candidate).Path
        if ($resolved -match "(?i)\\Windows\\System32\\bash\.exe$") {
            continue
        }
        return $resolved
    }
    throw "未找到 Git Bash。请安装 Git for Windows，或设置 NAUMI_GIT_BASH 指向 bin\bash.exe。"
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
Set-Location -LiteralPath $repoRoot

Write-Step "检查运行时"
$python = Require-Command "python"
$uv = Require-Command "uv"
$node = Require-Command "node"
$null = Require-Command "npm"
$gitBash = Resolve-GitBash

$pythonVersion = (& $python.Source --version 2>&1 | Select-Object -First 1)
if ($pythonVersion -notmatch "Python\s+(?<major>\d+)\.(?<minor>\d+)") {
    throw "无法识别 Python 版本：$pythonVersion"
}
if ([int]$Matches.major -lt 3 -or ([int]$Matches.major -eq 3 -and [int]$Matches.minor -lt 12)) {
    throw "NaumiAgent 需要 Python 3.12 或更高版本，当前为 $pythonVersion。"
}

$nodeVersion = (& $node.Source --version 2>&1 | Select-Object -First 1)
if ($nodeVersion -notmatch "^v(?<major>\d+)") {
    throw "无法识别 Node.js 版本：$nodeVersion"
}
if ([int]$Matches.major -lt 20) {
    throw "终端 UI 需要 Node.js 20 或更高版本，当前为 $nodeVersion。"
}

Write-Host "  Python: $pythonVersion"
Write-Host "  uv: $(& $uv.Source --version | Select-Object -First 1)"
Write-Host "  Node.js: $nodeVersion"
Write-Host "  Git Bash: $gitBash"

Write-Step "同步 Python 3.12 开发环境"
& $uv.Source sync --python 3.12 --extra dev
if ($LASTEXITCODE -ne 0) {
    throw "uv sync 执行失败，退出码：$LASTEXITCODE"
}

$configPath = Join-Path $repoRoot "config.yaml"
if (Test-Path -LiteralPath $configPath) {
    Write-Step "保留现有 config.yaml"
} else {
    Write-Step "创建无密钥 config.yaml"
    $config = @'
models:
  default_model: "openai/kimi-for-coding"
  fast_model: "openai/kimi-for-coding"
  reasoning_model: "openai/kimi-for-coding"
  max_tokens: 4096
  temperature: 1.0
  api_base: "https://api.kimi.com/coding/v1"
  model_info:
    openai/kimi-for-coding:
      max_context: 256000

memory:
  session_db_path: "data/sessions.db"
  vector_db_path: "data/chroma"
  compaction_threshold: 0.75

workspace_root: "."

safety:
  permission_mode: "moderate"
  allowed_dirs:
    - "."
  max_budget_usd: 5.0
  max_turns: 30
  max_input_tokens: 500000

mcp:
  servers: {}

api:
  host: "127.0.0.1"
  port: 8765
  api_keys: []
  cors_origins: ["*"]

browser_daemon:
  enabled: false
  base_url: "http://127.0.0.1:3005"

log_level: "INFO"
'@
    [System.IO.File]::WriteAllText(
        $configPath,
        $config + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
}

$userKey = [Environment]::GetEnvironmentVariable("NAUMI_MODELS__API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($userKey)) {
    throw @"
尚未设置 Windows 用户级 NAUMI_MODELS__API_KEY。
请在 PowerShell 中使用 [Environment]::SetEnvironmentVariable 将密钥写入 User 作用域，
然后重新打开终端并再次运行此脚本。不要把密钥写入 config.yaml 或命令历史。
"@
}

# A newly persisted user variable is not visible to an already-running terminal.
$env:NAUMI_MODELS__API_KEY = $userKey
$env:NAUMI_GIT_BASH = $gitBash

Write-Step "验证配置（不会显示密钥）"
$verifyCode = @'
from naumi_agent.config.settings import AppConfig

config = AppConfig.from_yaml('config.yaml')
assert config.models.api_key, 'Kimi API key was not loaded'
assert config.resolve_workspace_root().is_dir()
assert config.api.host == '127.0.0.1'
assert config.browser_daemon.enabled is False
print('配置验证通过：Kimi 密钥已加载，工作区与本地 API 配置有效。')
'@
& $uv.Source run python -c $verifyCode
if ($LASTEXITCODE -ne 0) {
    throw "NaumiAgent 配置验证失败，退出码：$LASTEXITCODE"
}

Write-Step "Windows 初始化完成"
Write-Host "  CLI:  uv run naumi chat"
Write-Host "  UI:   uv run naumi ui"
Write-Host "  API:  uv run naumi serve"
