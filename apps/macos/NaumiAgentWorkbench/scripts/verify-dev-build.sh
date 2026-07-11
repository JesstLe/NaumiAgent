#!/usr/bin/env bash
#
# verify-dev-build.sh — single release gate combining backend tests, the Mac
# Workbench local-loop smoke, the Swift test suite, and dev app packaging.
#
# Fails fast on the first broken stage. Intended for CI and pre-release checks;
# runs without network access beyond localhost and needs no notarization.
#
# Usage:
#   ./scripts/verify-dev-build.sh                # full gate
#   ./scripts/verify-dev-build.sh --skip-package # skip the .app packaging stage
set -euo pipefail

cd "$(dirname "$0")/../../../.."   # repo root

skip_package=0
for arg in "$@"; do
  case "$arg" in
    --skip-package) skip_package=1 ;;
    *) echo "用法: ./scripts/verify-dev-build.sh [--skip-package]" >&2; exit 64 ;;
  esac
done

fail() {
  echo "❌ 验证失败: $1" >&2
  exit 1
}

echo "==> [1/4] ruff 静态检查"
ruff check src/ tests/ || fail "ruff 检查未通过"

echo "==> [2/4] Python 后端 + 本地闭环冒烟"
.venv/bin/python -m pytest \
  tests/unit/test_workbench_models.py \
  tests/unit/test_workbench_store.py \
  tests/unit/test_workbench_service.py \
  tests/unit/test_workbench_export.py \
  tests/unit/test_api_workbench.py \
  tests/e2e/test_mac_workbench_local_loop.py \
  -q || fail "Python 测试未通过"

echo "==> [3/4] Swift 测试套件"
(
  cd apps/macos/NaumiAgentWorkbench
  ./scripts/test.sh
) || fail "Swift 测试未通过"

if [[ "$skip_package" -eq 1 ]]; then
  echo "==> [4/4] 跳过打包（--skip-package）"
else
  echo "==> [4/4] 开发 app 打包冒烟"
  (
    cd apps/macos/NaumiAgentWorkbench
    ./scripts/package-dev-app.sh
  ) || fail "开发打包未通过"
fi

echo ""
echo "✅ 全部验证通过：ruff + 后端测试 + 本地闭环冒烟 + Swift 测试${skip_package:+（已跳过打包）}"
