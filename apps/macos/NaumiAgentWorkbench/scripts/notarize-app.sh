#!/usr/bin/env bash
#
# notarize-app.sh — submit a signed NaumiAgentWorkbench zip to Apple notarization
# and staple the ticket to the app bundle.
#
# Prerequisites:
#   - An app-specific password stored in the keychain under an app-specific
#     profile. Create it once:
#       xcrun notarytool store-credentials "naumi-notary" \
#         --apple-id "you@example.com" \
#         --team-id "TEAMID" \
#         --password
#   - A signed zip produced by package-signed-app.sh.
#
# Required environment:
#   NAUMI_NOTARY_KEYCHAIN_PROFILE   keychain profile name (default "naumi-notary")
#
# Usage:
#   ./scripts/notarize-app.sh dist/NaumiAgentWorkbench-signed.zip
set -euo pipefail

cd "$(dirname "$0")/.."

zip_path="${1:-}"
profile="${NAUMI_NOTARY_KEYCHAIN_PROFILE:-naumi-notary}"
app_name="NaumiAgentWorkbench"
dist_dir="dist"

if [[ -z "$zip_path" ]]; then
  echo "用法: ./scripts/notarize-app.sh <signed-zip-path>" >&2
  echo "示例: ./scripts/notarize-app.sh dist/${app_name}-signed.zip" >&2
  exit 64
fi

if [[ ! -f "$zip_path" ]]; then
  echo "错误: 未找到 zip 文件 ${zip_path}" >&2
  exit 1
fi

echo "==> 提交公证（profile: ${profile}）"
submission_id="$(xcrun notarytool submit "$zip_path" \
  --keychain-profile "$profile" \
  --wait \
  --format json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' || true)"

echo "==> 获取公证结果"
# Re-run to capture final status; --wait blocks until completion.
xcrun notarytool submit "$zip_path" \
  --keychain-profile "$profile" \
  --wait

# Fetch the most recent submission's status and fail on rejection.
echo "==> 校验公证状态"
latest_json="$(xcrun notarytool history \
  --keychain-profile "$profile" \
  --format json 2>/dev/null || true)"
status="$(echo "$latest_json" | python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("history",[{}])[0]).get("status",""))' 2>/dev/null || echo "")"

if [[ "$status" != "Accepted" ]]; then
  echo "❌ 公证未通过（状态: ${status:-未知}）" >&2
  echo "查看详情: xcrun notarytool log <submission-id> --keychain-profile ${profile}" >&2
  exit 1
fi

echo "✅ 公证通过"

app_dir="${dist_dir}/${app_name}.app"
if [[ -d "$app_dir" ]]; then
  echo "==> 装订票据到 app bundle"
  xcrun stapler staple "$app_dir"
  echo "==> 校验装订"
  xcrun stapler validate "$app_dir"
fi

echo ""
echo "✅ 公证与装订完成"
echo "   app:  ${app_dir}"
echo "   分发前确认: spctl --assess -vv ${app_dir}"
