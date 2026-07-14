#!/usr/bin/env bash
# NaumiAgent binary installer for macOS and Linux.

set -euo pipefail

RELEASE_REPO=${NAUMI_RELEASE_REPO:-JesstLe/NaumiAgent-Releases}
VERSION=${NAUMI_VERSION:-latest}
INSTALL_ROOT=${NAUMI_INSTALL_ROOT:-$HOME/.local/share/naumi-agent}
BIN_DIR=${NAUMI_BIN_DIR:-$HOME/.local/bin}

info() { printf '\033[36m[naumi]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[naumi]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[naumi]\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "缺少 curl，无法下载安装包。"
command -v tar >/dev/null 2>&1 || fail "缺少 tar，无法解压安装包。"

case "$(uname -s)" in
    Darwin) platform=macos ;;
    Linux) platform=linux ;;
    *) fail "当前安装器仅支持 macOS 和 Linux；Windows 请使用 install.ps1。" ;;
esac
case "$(uname -m)" in
    x86_64|amd64) arch=x64 ;;
    arm64|aarch64) arch=arm64 ;;
    *) fail "不支持的处理器架构：$(uname -m)" ;;
esac

if [ -n "${NAUMI_RELEASE_BASE_URL:-}" ]; then
    base_url=${NAUMI_RELEASE_BASE_URL%/}
elif [ "$VERSION" = "latest" ]; then
    base_url="https://github.com/$RELEASE_REPO/releases/latest/download"
else
    case "$VERSION" in
        *[!A-Za-z0-9._-]*|'') fail "NAUMI_VERSION 含不安全字符。" ;;
    esac
    base_url="https://github.com/$RELEASE_REPO/releases/download/v$VERSION"
fi

if [ "$VERSION" = "latest" ]; then
    asset="naumi-$platform-$arch.tar.gz"
else
    asset="naumi-$VERSION-$platform-$arch.tar.gz"
fi

tmp=$(mktemp -d "${TMPDIR:-/tmp}/naumi-install.XXXXXX")
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT INT TERM

info "下载 $asset"
curl --fail --location --proto '=https' --tlsv1.2 \
    "$base_url/$asset" --output "$tmp/$asset"
curl --fail --location --proto '=https' --tlsv1.2 \
    "$base_url/$asset.sha256" --output "$tmp/$asset.sha256"

expected=$(awk 'NR == 1 { print $1 }' "$tmp/$asset.sha256")
case "$expected" in
    *[!0-9A-Fa-f]*|'') fail "checksum 文件格式无效。" ;;
esac
[ "${#expected}" -eq 64 ] || fail "checksum 长度无效。"
if command -v shasum >/dev/null 2>&1; then
    actual=$(shasum -a 256 "$tmp/$asset" | awk '{ print $1 }')
elif command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$tmp/$asset" | awk '{ print $1 }')
else
    fail "缺少 shasum 或 sha256sum，不能安全校验下载。"
fi
[ "$actual" = "$expected" ] || fail "SHA-256 校验失败，已拒绝安装。"

tar -tzf "$tmp/$asset" > "$tmp/archive.list" \
    || fail "无法读取安装包目录。"
while IFS= read -r entry; do
    case "$entry" in
        /*|../*|*/../*|*/..) fail "安装包含不安全路径：$entry" ;;
        naumi-*-"$platform-$arch"|naumi-*-"$platform-$arch"/|naumi-*-"$platform-$arch"/*) ;;
        *) fail "安装包含契约外路径：$entry" ;;
    esac
done < "$tmp/archive.list"

mkdir "$tmp/extract"
tar -xzf "$tmp/$asset" -C "$tmp/extract"
set -- "$tmp"/extract/naumi-*-$platform-$arch
[ "$#" -eq 1 ] && [ -d "$1" ] || fail "安装包顶层目录不符合发行契约。"
bundle=$1
[ -f "$bundle/manifest.json" ] || fail "安装包缺少 manifest.json。"
[ -x "$bundle/naumi" ] || fail "安装包缺少可执行后端。"
[ -x "$bundle/naumi-ui" ] || fail "安装包缺少可执行 Terminal UI。"

mkdir -p "$INSTALL_ROOT/releases" "$BIN_DIR"
destination="$INSTALL_ROOT/releases/$(basename "$bundle")"
[ ! -e "$destination" ] || fail "该版本已安装：$destination"
staged="$INSTALL_ROOT/.install-$(basename "$bundle")-$$"
mv "$bundle" "$staged"
mv "$staged" "$destination"

rm -f "$INSTALL_ROOT/current.new" "$BIN_DIR/naumi.new"
ln -s "$destination" "$INSTALL_ROOT/current.new"
mv -f "$INSTALL_ROOT/current.new" "$INSTALL_ROOT/current"
ln -s "$INSTALL_ROOT/current/naumi" "$BIN_DIR/naumi.new"
mv -f "$BIN_DIR/naumi.new" "$BIN_DIR/naumi"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR 尚未在 PATH；请加入 shell 配置后重新打开终端。"
fi
info "安装完成：$destination"
info "运行：naumi"
