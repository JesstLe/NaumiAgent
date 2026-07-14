#!/usr/bin/env bash

set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

VERSION=${VERSION:-$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -1)}
TARGET=${TARGET:-}
OUTPUT_DIR=${OUTPUT_DIR:-dist/release}

if [ -z "$VERSION" ]; then
    printf '无法从 pyproject.toml 读取版本。\n' >&2
    exit 1
fi
if [ -z "$TARGET" ]; then
    os=$(uname -s)
    arch=$(uname -m)
    case "$os" in
        Darwin) platform=macos ;;
        Linux) platform=linux ;;
        *) printf '不支持的构建系统：%s\n' "$os" >&2; exit 1 ;;
    esac
    case "$arch" in
        x86_64|amd64) arch=x64 ;;
        arm64|aarch64) arch=arm64 ;;
        *) printf '不支持的构建架构：%s\n' "$arch" >&2; exit 1 ;;
    esac
    TARGET="$platform-$arch"
fi

command -v bun >/dev/null 2>&1 || { printf '缺少 bun。\n' >&2; exit 1; }
command -v pyinstaller >/dev/null 2>&1 || { printf '缺少 pyinstaller。\n' >&2; exit 1; }

rm -rf build/naumi dist/naumi dist/naumi-ui
bun build frontend/terminal-ui/src/index.js --compile --outfile dist/naumi-ui
dist/naumi-ui --self-test
pyinstaller --noconfirm --clean packaging/naumi.spec
dist/naumi/naumi --help >/dev/null

python_cmd=${PYTHON:-python3}
"$python_cmd" scripts/release/verify_frozen_bridge.py dist/naumi/naumi
"$python_cmd" scripts/release/assemble_artifact.py \
    --backend-dir dist/naumi \
    --ui-binary dist/naumi-ui \
    --config-example config.yaml.example \
    --output-dir "$OUTPUT_DIR" \
    --version "$VERSION" \
    --target "$TARGET" \
    --archive-format tar.gz
