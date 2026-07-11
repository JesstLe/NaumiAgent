#!/usr/bin/env bash
#
# package-signed-app.sh — build and Developer-ID-sign NaumiAgentWorkbench.app
# for internal distribution.
#
# Unlike package-dev-app.sh (ad-hoc, non-notarized), this script signs the bundle
# with a real Developer ID Application certificate so it can be notarized and
# distributed to other Macs. Notarization is a separate step — run
# notarize-app.sh after this.
#
# Required environment:
#   NAUMI_SIGNING_IDENTITY   Developer ID Application: ... (from keychain)
#
# Optional environment:
#   NAUMI_BUNDLE_VERSION     version string (default "1")
#   NAUMI_INCLUDE_FIXTURES   "1" to bundle preview fixtures (default off)
#
# Usage:
#   NAUMI_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#     ./scripts/package-signed-app.sh
set -euo pipefail

cd "$(dirname "$0")/.."

app_name="NaumiAgentWorkbench"
bundle_id="${NAUMI_BUNDLE_ID:-ai.naumi.workbench}"
bundle_version="${NAUMI_BUNDLE_VERSION:-1}"
min_system_version="14.0"
signing_identity="${NAUMI_SIGNING_IDENTITY:-}"
include_fixtures="${NAUMI_INCLUDE_FIXTURES:-0}"
dist_dir="dist"

if [[ -z "$signing_identity" ]]; then
  echo "错误: 必须设置 NAUMI_SIGNING_IDENTITY 环境变量" >&2
  echo "示例: NAUMI_SIGNING_IDENTITY=\"Developer ID Application: Your Name (TEAMID)\"" >&2
  echo "查看可用身份: security find-identity -v -p codesigning" >&2
  exit 64
fi

echo "==> 校验签名身份是否可用: ${signing_identity}"
if ! security find-identity -v -p codesigning | grep -q "$signing_identity"; then
  echo "错误: 在钥匙串中未找到签名身份「${signing_identity}」" >&2
  echo "请确认 Developer ID Application 证书已导入并解锁" >&2
  exit 1
fi

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

if [[ "$include_fixtures" == "1" ]]; then
  echo "==> 包含预览 fixture"
  mkdir -p "${app_dir}/Contents/Resources/Fixtures"
  cp Fixtures/workbench_snapshot_*.json "${app_dir}/Contents/Resources/Fixtures/" 2>/dev/null || true
fi

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

echo "==> 使用 Developer ID 签名: ${signing_identity}"
# Hardened runtime + timestamp are required for notarization.
codesign --force --deep \
  --options runtime \
  --timestamp \
  --sign "$signing_identity" \
  "$app_dir"

echo "==> 校验签名与权限"
codesign --verify --strict --verbose=2 "$app_dir"
spctl --assess --type execute --verbose "$app_dir" || {
  echo "警告: Gatekeeper 评估未通过（spctl）。公证后通常会通过。" >&2
}

zip_path="${dist_dir}/${app_name}-signed.zip"
echo "==> 压缩（供公证上传）: ${zip_path}"
rm -f "$zip_path"
ditto -c -k --keepParent "$app_dir" "$zip_path"

echo ""
echo "✅ 签名打包完成"
echo "   app: ${app_dir}"
echo "   zip: ${zip_path}"
echo "   下一步: ./scripts/notarize-app.sh ${zip_path}"
