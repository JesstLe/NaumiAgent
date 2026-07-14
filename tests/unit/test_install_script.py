from __future__ import annotations

from pathlib import Path


def _script() -> str:
    path = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"
    return path.read_text(encoding="utf-8")


def test_install_script_uses_real_repository_without_placeholders() -> None:
    script = _script()

    assert "github.com/JesstLe/NaumiAgent.git" in script
    assert "your-org" not in script


def test_install_script_validates_python_without_platform_specific_sort() -> None:
    script = _script()

    assert "sys.version_info >= (3, 12)" in script
    assert "sort -V" not in script


def test_install_script_keeps_textual_available_without_node() -> None:
    script = _script()

    assert "terminal_ui_available=0" in script
    assert "terminal_ui_available=1" in script
    assert 'if [ "$terminal_ui_available" = 1 ]' in script
    assert "将使用 Textual TUI fallback" in script
    assert 'log_info "  naumi tui"' in script
    assert "naumi chat --classic" not in script
    assert "naumi ui --legacy" not in script


def test_install_script_survives_broken_node_probe_and_npm_install() -> None:
    script = _script()

    assert "if node_version=$(node -p 'process.versions.node' 2>/dev/null); then" in script
    assert 'case "$node_major" in' in script
    assert 'if (cd "$ui_dir" && npm install --no-audit --no-fund); then' in script
    assert "Node UI 依赖安装失败，将使用 Textual TUI fallback" in script


def test_install_script_installs_managed_browser_runtime() -> None:
    script = _script()

    assert '.venv/bin/playwright install chromium' in script


def test_install_script_does_not_hide_repository_update_failures() -> None:
    assert "git pull --ff-only || true" not in _script()


def test_install_script_updates_shell_path_idempotently() -> None:
    script = _script()

    assert "grep -Fqx" in script


def test_readme_declares_terminal_ui_as_default_entry() -> None:
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")

    assert "`naumi` 默认启动新一代 Node Terminal UI" in readme
    assert "启动失败时自动回退到 Textual TUI" in readme
    assert "naumi tui" in readme
    assert "naumi chat --classic" not in readme
    assert "Prompt Toolkit 兼容 CLI" not in readme
    assert "主入口是全屏 CLI：`naumi chat`" not in readme
    assert "可选安装 Node UI 依赖" not in readme
