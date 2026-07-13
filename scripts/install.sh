#!/usr/bin/env bash
# NaumiAgent 一键安装脚本
# 用法: curl -sSL https://raw.githubusercontent.com/JesstLe/NaumiAgent/main/scripts/install.sh | bash
# 环境变量:
#   INSTALL_DIR   安装目录，默认 ~/.naumi-agent
#   BIN_DIR       可执行文件软链目录，默认 ~/.local/bin
#   USE_UV        1 强制使用 uv，0 强制使用 pip，默认自动检测

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/JesstLe/NaumiAgent.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.naumi-agent}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

log_info() { printf '\033[36m[naumi]\033[0m %s\n' "$*"; }
log_warn() { printf '\033[33m[naumi]\033[0m %s\n' "$*"; }
log_error() { printf '\033[31m[naumi]\033[0m %s\n' "$*"; }

# 1. 检测 Python
python_cmd=""
for cmd in python3.12 python3.13 python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        if "$cmd" -c 'import sys; raise SystemExit(not (sys.version_info >= (3, 12)))' 2>/dev/null; then
            python_cmd=$cmd
            break
        fi
    fi
done

if [ -z "$python_cmd" ]; then
    log_error "未找到 Python 3.12+。请安装后重试："
    log_error "  macOS: brew install python@3.12"
    log_error "  Ubuntu: sudo apt install python3.12 python3.12-venv"
    exit 1
fi
log_info "使用 Python: $python_cmd"

# 2. 决定包管理器
if [ "${USE_UV:-}" = "1" ]; then
    use_uv=1
elif [ "${USE_UV:-}" = "0" ]; then
    use_uv=0
elif command -v uv >/dev/null 2>&1; then
    use_uv=1
else
    use_uv=0
fi

if [ "$use_uv" = 1 ]; then
    log_info "使用 uv 安装依赖"
else
    log_warn "未检测到 uv，使用 pip。推荐安装 uv 以获得更快体验："
    log_warn "  curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# 3. 下载源码
if [ -d "$INSTALL_DIR/.git" ]; then
    log_info "更新已有安装目录 $INSTALL_DIR"
    cd "$INSTALL_DIR"
    git pull --ff-only
else
    log_info "克隆源码到 $INSTALL_DIR"
    if command -v git >/dev/null 2>&1; then
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    else
        log_error "需要 git 才能克隆仓库。请先安装 git。"
        exit 1
    fi
fi

cd "$INSTALL_DIR"

# 4. 安装 Python 依赖
if [ "$use_uv" = 1 ]; then
    uv sync --no-dev
    naumi_bin="$INSTALL_DIR/.venv/bin/naumi"
else
    "$python_cmd" -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -e "."
    naumi_bin="$INSTALL_DIR/.venv/bin/naumi"
fi

log_info "安装 Chromium 浏览器运行时..."
if ! .venv/bin/playwright install chromium; then
    log_warn "Chromium 下载失败；运行时将尝试使用系统 Chrome/Edge。"
fi

# 5. 创建命令入口
mkdir -p "$BIN_DIR"
ln -sf "$naumi_bin" "$BIN_DIR/naumi"
log_info "已创建 $BIN_DIR/naumi -> $naumi_bin"

# 6. 确保 PATH 包含 BIN_DIR
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    shell_rc=""
    case "${SHELL:-}" in
        */zsh) shell_rc="$HOME/.zshrc" ;;
        */bash) shell_rc="$HOME/.bashrc" ;;
    esac
    if [ -n "$shell_rc" ]; then
        path_line="export PATH=\"$BIN_DIR:\$PATH\""
        if ! grep -Fqx "$path_line" "$shell_rc" 2>/dev/null; then
            printf '%s\n' "$path_line" >> "$shell_rc"
            log_info "已将 $BIN_DIR 加入 $shell_rc"
        fi
    else
        log_warn "请将 $BIN_DIR 加入你的 PATH"
    fi
fi

# 7. Node.js 检查（默认终端 UI 必需）
if ! command -v node >/dev/null 2>&1; then
    log_error "未检测到 Node.js 20+。默认终端 UI 无法启动。"
    exit 1
fi
node_version=$(node -p 'process.versions.node')
node_major=${node_version%%.*}
if [ "$node_major" -lt 20 ]; then
    log_error "Node.js 20+ 为必需，当前版本为 $node_version。"
    exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
    log_error "检测到 Node.js，但未找到 npm。请安装完整 Node.js 20+ 发行版。"
    exit 1
fi
log_info "检测到 Node.js $node_version"
ui_dir="$INSTALL_DIR/frontend/terminal-ui"
log_info "安装 Node UI 依赖..."
(cd "$ui_dir" && npm install --no-audit --no-fund)
log_info "Node UI 依赖安装完成"

log_info "安装完成"
log_info "首次运行请执行:"
log_info "  export PATH=\"$BIN_DIR:\$PATH\"  # 如未自动生效"
log_info "  naumi"
log_info "兼容入口:"
log_info "  naumi chat --classic"
log_info "  naumi ui --legacy"
