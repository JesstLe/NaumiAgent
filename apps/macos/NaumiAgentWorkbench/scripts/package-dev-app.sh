#!/usr/bin/env bash
#
# package-dev-app.sh — build a local, non-notarized NaumiAgentWorkbench.app
# bundle for internal development use.
#
# This is NOT for public distribution. The resulting app is ad-hoc signed (or
# Developer ID signed when NAUMI_SIGNING_IDENTITY is set) and launches against a
# local naumi-agent daemon at http://127.0.0.1:8765 by default.
#
# Usage:
#   ./scripts/package-dev-app.sh                  # minimal release app, no fixtures
#   ./scripts/package-dev-app.sh --include-fixtures
#   ./scripts/package-dev-app.sh --include-fixtures --open
#
# Outputs:
#   dist/NaumiAgentWorkbench.app
#   dist/NaumiAgentWorkbench-dev.zip
set -euo pipefail

cd "$(dirname "$0")/.."

app_name="NaumiAgentWorkbench"
bundle_id="ai.naumi.workbench"
bundle_version="${NAUMI_BUNDLE_VERSION:-1}"
min_system_version="14.0"
dist_dir="dist"
include_fixtures=0
open_after=0

for arg in "$@"; do
  case "$arg" in
    --include-fixtures)
      include_fixtures=1
      ;;
    --open)
      open_after=1
      ;;
    *)
      echo "用法: ./scripts/package-dev-app.sh [--include-fixtures] [--open]" >&2
      echo "  --include-fixtures  将预览 fixture 复制进 app 包（仅用于离线预览）" >&2
      echo "  --open              打包后立即打开 app" >&2
      exit 64
      ;;
  esac
done

echo "==> 构建发布版本 (release)"
swift build -c release

build_bin=".build/release/${app_name}"
if [[ ! -f "$build_bin" ]]; then
  echo "错误: 未找到 release 可执行文件 $build_bin" >&2
  exit 1
fi

app_dir="${dist_dir}/${app_name}.app"
echo "==> 组装 app bundle: ${app_dir}"
rm -rf "$app_dir"
mkdir -p "${app_dir}/Contents/MacOS" "${app_dir}/Contents/Resources"

cp "$build_bin" "${app_dir}/Contents/MacOS/${app_name}"

# Copy preview fixtures only when explicitly requested. Real mode does not need
# them and keeping them out reduces bundle size and avoids showing fixture rows
# in a real daemon session.
if [[ "$include_fixtures" -eq 1 ]]; then
  echo "==> 包含预览 fixture"
  mkdir -p "${app_dir}/Contents/Resources/Fixtures"
  cp Fixtures/workbench_snapshot_*.json "${app_dir}/Contents/Resources/Fixtures/" 2>/dev/null || true
fi

# Minimal Info.plist sufficient for a dev bundle (no sandbox, no notarization).
cat > "${app_dir}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>${app_name}</string>
  <key>CFBundleIdentifier</key>
  <string>${bundle_id}</string>
  <key>CFBundleName</key>
  <string>${app_name}</string>
  <key>CFBundleDisplayName</key>
  <string>NaumiAgent Workbench</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleVersion</key>
  <string>${bundle_version}</string>
  <key>CFBundleShortVersionString</key>
  <string>${bundle_version}</string>
  <key>LSMinimumSystemVersion</key>
  <string>${min_system_version}</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>NaumiAgent Workbench 仅连接本机的 NaumiAgent 服务（127.0.0.1）。</string>
</dict>
</plist>
PLIST

# Sign the bundle. Use a Developer ID identity when provided; otherwise ad-hoc
# sign so the app launches on the build machine without a quarantine prompt.
signing_identity="${NAUMI_SIGNING_IDENTITY:-}"
if [[ -n "$signing_identity" ]]; then
  echo "==> 使用签名身份签名: ${signing_identity}"
  codesign --force --deep --sign "$signing_identity" "$app_dir"
else
  echo "==> ad-hoc 签名（本地开发使用）"
  codesign --force --deep --sign - "$app_dir"
fi

verify="$(codesign --verify --verbose=1 "$app_dir" 2>&1 || true)"
echo "$verify"

zip_path="${dist_dir}/${app_name}-dev.zip"
echo "==> 压缩: ${zip_path}"
# Use ditto to preserve macOS metadata and bundle structure.
rm -f "$zip_path"
ditto -c -k --keepParent "$app_dir" "$zip_path"

echo ""
echo "✅ 打包完成"
echo "   app:  ${app_dir}"
echo "   zip:  ${zip_path}"
echo "   启动: open ${app_dir}"
if [[ "$open_after" -eq 1 ]]; then
  /usr/bin/open "$app_dir"
fi
