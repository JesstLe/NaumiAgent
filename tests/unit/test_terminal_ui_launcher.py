from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.main import TerminalUiLaunchError, _build_terminal_ui_command


def test_build_terminal_ui_command_uses_direct_node_entry(tmp_path: Path) -> None:
    frontend = tmp_path / "terminal-ui"
    entry = frontend / "src" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")

    cmd = _build_terminal_ui_command(
        "config.yaml",
        frontend_dir=frontend,
        node_executable="/opt/bin/node",
    )

    assert cmd == ["/opt/bin/node", str(entry), "--config", "config.yaml"]


def test_build_terminal_ui_command_requires_entry(tmp_path: Path) -> None:
    with pytest.raises(TerminalUiLaunchError, match="未找到新终端 UI 入口"):
        _build_terminal_ui_command(
            "config.yaml",
            frontend_dir=tmp_path / "missing-ui",
            node_executable="/opt/bin/node",
        )


def test_build_terminal_ui_command_requires_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "terminal-ui"
    entry = frontend / "src" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    monkeypatch.setattr("naumi_agent.main.shutil.which", lambda name: None)

    with pytest.raises(TerminalUiLaunchError, match="未找到 Node.js"):
        _build_terminal_ui_command("config.yaml", frontend_dir=frontend)
