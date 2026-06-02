from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.main import TerminalUiLaunchError, _build_terminal_ui_command, _launch_terminal_ui


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


def test_launch_terminal_ui_preserves_invocation_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(
        "naumi_agent.main._build_terminal_ui_command",
        lambda config_path: ["/opt/bin/node", "index.js", "--config", config_path],
    )

    def fake_run(cmd: list[str], *, cwd: str, check: bool) -> SimpleNamespace:
        calls.append({"cmd": cmd, "cwd": cwd, "check": check})
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr("naumi_agent.main.subprocess.run", fake_run)

    assert _launch_terminal_ui("local-config.yaml", cwd=workspace) == 7
    assert calls == [
        {
            "cmd": ["/opt/bin/node", "index.js", "--config", "local-config.yaml"],
            "cwd": str(workspace),
            "check": False,
        }
    ]
