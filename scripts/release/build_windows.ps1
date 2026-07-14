$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

$Version = if ($env:VERSION) { $env:VERSION } else {
    $Match = Select-String -Path "pyproject.toml" -Pattern '^version = "([^"]+)"' | Select-Object -First 1
    if (-not $Match) { throw "无法从 pyproject.toml 读取版本。" }
    $Match.Matches[0].Groups[1].Value
}
$Target = if ($env:TARGET) { $env:TARGET } else { "windows-x64" }
$OutputDir = if ($env:OUTPUT_DIR) { $env:OUTPUT_DIR } else { "dist\release" }

if (-not (Get-Command bun -ErrorAction SilentlyContinue)) { throw "缺少 bun。" }
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) { throw "缺少 pyinstaller。" }

Remove-Item -Recurse -Force "build\naumi", "dist\naumi" -ErrorAction SilentlyContinue
Remove-Item -Force "dist\naumi-ui.exe" -ErrorAction SilentlyContinue
bun build "frontend/terminal-ui/src/index.js" --compile --outfile "dist/naumi-ui.exe"
& "dist/naumi-ui.exe" --self-test
pyinstaller --noconfirm --clean "packaging/naumi.spec"
& "dist/naumi/naumi.exe" --help | Out-Null
python "scripts/release/verify_frozen_bridge.py" "dist/naumi/naumi.exe"

python "scripts/release/assemble_artifact.py" `
    --backend-dir "dist/naumi" `
    --ui-binary "dist/naumi-ui.exe" `
    --config-example "config.yaml.example" `
    --output-dir $OutputDir `
    --version $Version `
    --target $Target `
    --archive-format zip
