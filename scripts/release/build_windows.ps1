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
$SigningKey = $env:NAUMI_RELEASE_BUILDER_PRIVATE_KEY_BASE64
Remove-Item Env:NAUMI_RELEASE_BUILDER_PRIVATE_KEY_BASE64 -ErrorAction SilentlyContinue

if (-not $SigningKey) { throw "缺少可信构建签名私钥。" }

if (-not (Get-Command bun -ErrorAction SilentlyContinue)) { throw "缺少 bun。" }
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) { throw "缺少 pyinstaller。" }

Remove-Item -Recurse -Force "build\naumi", "build\naumi-runtime", "build\naumi_launcher", "dist\naumi-runtime" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "dist\naumi-launcher" -ErrorAction SilentlyContinue
Remove-Item -Force "dist\naumi-ui.exe" -ErrorAction SilentlyContinue
bun build "frontend/terminal-ui/src/index.js" --compile --outfile "dist/naumi-ui.exe"
& "dist/naumi-ui.exe" --self-test
pyinstaller --noconfirm --clean "packaging/naumi.spec"
pyinstaller --noconfirm --clean "packaging/naumi_launcher.spec"
& "dist/naumi-runtime/naumi-runtime.exe" --help | Out-Null
& "dist/naumi-launcher/naumi.exe" --launcher-self-test | Out-Null
python "scripts/release/verify_frozen_bridge.py" "dist/naumi-runtime/naumi-runtime.exe"

try {
    $env:NAUMI_RELEASE_BUILDER_PRIVATE_KEY_BASE64 = $SigningKey
    python "scripts/release/assemble_artifact.py" `
        --backend-dir "dist/naumi-runtime" `
        --launcher-dir "dist/naumi-launcher" `
        --ui-binary "dist/naumi-ui.exe" `
        --config-example "config.yaml.example" `
        --output-dir $OutputDir `
        --version $Version `
        --target $Target `
        --archive-format zip
}
finally {
    Remove-Item Env:NAUMI_RELEASE_BUILDER_PRIVATE_KEY_BASE64 -ErrorAction SilentlyContinue
    $SigningKey = $null
}
