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


def test_install_script_requires_supported_node_for_default_ui() -> None:
    script = _script()

    assert "Node.js 20+" in script
    assert "node_major" in script
    assert 'if [ "$node_major" -lt 20 ]' in script


def test_install_script_does_not_hide_repository_update_failures() -> None:
    assert "git pull --ff-only || true" not in _script()


def test_install_script_updates_shell_path_idempotently() -> None:
    script = _script()

    assert "grep -Fqx" in script
    assert "naumi chat --classic" in script
    assert "naumi ui --legacy" in script
