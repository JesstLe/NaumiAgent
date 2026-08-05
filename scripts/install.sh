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
command -v diff >/dev/null 2>&1 || fail "缺少 diff，无法校验已安装 Launcher。"

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
[ -x "$bundle/launcher/naumi" ] || fail "安装包缺少稳定 Launcher。"
[ -x "$bundle/naumi-runtime" ] || fail "安装包缺少可执行 Runtime。"
[ -x "$bundle/naumi-ui" ] || fail "安装包缺少可执行 Terminal UI。"

launchers_dir="$INSTALL_ROOT/launchers"
launcher_destination="$launchers_dir/$(basename "$bundle")"
launcher="$launcher_destination/naumi"
mkdir -p "$launchers_dir" "$BIN_DIR"
if [ -e "$launcher_destination" ]; then
    diff -qr "$bundle/launcher" "$launcher_destination" >/dev/null \
        || fail "同版本 Launcher 内容冲突，拒绝覆盖：$launcher_destination"
else
    launcher_staged="$launchers_dir/.install-$(basename "$bundle")-$$"
    rm -rf "$launcher_staged"
    cp -R "$bundle/launcher" "$launcher_staged"
    mv "$launcher_staged" "$launcher_destination"
    find "$launcher_destination" -type d -exec chmod 555 {} +
    find "$launcher_destination" -type f -exec chmod 444 {} +
    chmod 555 "$launcher"
fi
"$launcher" --launcher-self-test >/dev/null \
    || fail "稳定 Launcher 自检失败，拒绝安装。"
if ! NAUMI_INSTALL_ROOT="$INSTALL_ROOT" "$launcher" --launcher-install "$bundle"; then
    fail "版本槽安装或激活失败，PATH 仍指向上一稳定 Launcher。"
fi
rm -f "$BIN_DIR/naumi.new"
ln -s "$launcher" "$BIN_DIR/naumi.new"
mv -f "$BIN_DIR/naumi.new" "$BIN_DIR/naumi"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR 尚未在 PATH；请加入 shell 配置后重新打开终端。"
fi
info "安装完成：active version slot 已原子切换。"
info "运行：naumi"
