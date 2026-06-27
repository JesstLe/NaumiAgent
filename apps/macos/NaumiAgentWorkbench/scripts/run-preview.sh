#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

locale="${1:-zh}"
route="${2:-}"
case "$locale" in
  zh | zh-CN | zh_cn)
    fixture_locale="zh"
    ;;
  en | en-US | en_us)
    fixture_locale="en"
    ;;
  *)
    echo "用法: ./scripts/run-preview.sh [zh|en] [dashboard|task-market|worktrees|reviews|timeline|settings]" >&2
    exit 64
    ;;
esac

bundle_dir="${TMPDIR:-/tmp}/NaumiAgentWorkbenchPreview.app"

swift build

rm -rf "$bundle_dir"
mkdir -p "$bundle_dir/Contents/MacOS" "$bundle_dir/Contents/Resources/Fixtures"
cp ".build/debug/NaumiAgentWorkbench" "$bundle_dir/Contents/MacOS/NaumiAgentWorkbench"
cp Fixtures/workbench_snapshot_*.json "$bundle_dir/Contents/Resources/Fixtures/"

cat > "$bundle_dir/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>NaumiAgentWorkbench</string>
  <key>CFBundleIdentifier</key>
  <string>ai.naumi.workbench.preview</string>
  <key>CFBundleName</key>
  <string>NaumiAgentWorkbenchPreview</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>14.0</string>
</dict>
</plist>
PLIST

args=(--preview-fixture "$fixture_locale")
if [[ -n "$route" ]]; then
  args+=(--preview-route "$route")
fi

/usr/bin/open -n "$bundle_dir" --args "${args[@]}"
echo "已打开 NaumiAgentWorkbench 预览模式: $fixture_locale ${route:+route=$route}"
