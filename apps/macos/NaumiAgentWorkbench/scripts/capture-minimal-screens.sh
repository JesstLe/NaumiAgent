#!/usr/bin/env bash
# Captures the minimal real-mode fixture (sparse session, no fake fillers) for
# the UI audit. Use this to verify that a real empty session shows empty states
# instead of fabricated rows.
#
# Usage: ./scripts/capture-minimal-screens.sh zh [out_dir]
set -euo pipefail

cd "$(dirname "$0")/.."

locale="${1:-zh}"
out_dir="${2:-../../../docs/mac-app/ui-audit/screenshots/minimal}"

swift run NaumiAgentWorkbenchSnapshot \
  --locale "$locale" \
  --out "$out_dir" \
  --fixtures Fixtures \
  --fixture-name "workbench_snapshot_minimal_${locale}" \
  --width 1440 \
  --height 900 \
  --route all
