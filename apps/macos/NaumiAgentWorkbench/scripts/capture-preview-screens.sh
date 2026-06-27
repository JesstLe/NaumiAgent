#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

locale="${1:-zh}"
out_dir="${2:-../../../docs/mac-app/ui-audit/screenshots}"

swift run NaumiAgentWorkbenchSnapshot \
  --locale "$locale" \
  --out "$out_dir" \
  --fixtures Fixtures \
  --width 1440 \
  --height 900 \
  --route all
