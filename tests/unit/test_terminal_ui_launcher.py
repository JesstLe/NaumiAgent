from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from naumi_agent.main import (
    TerminalUiLaunchError,
    _build_terminal_ui_command,
    _launch_terminal_ui,
    _parse_node_major,
    _resolve_terminal_ui_frontend_dir,
)
from naumi_agent.main import (
    app as naumi_app,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fake_supported_node_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "naumi_agent.main.subprocess.check_output",
        lambda *args, **kwargs: "v20.11.1\n",
    )


def _write_terminal_ui_entry(frontend: Path) -> Path:
    entry = frontend / "src" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    return entry


def test_build_terminal_ui_command_uses_direct_node_entry(tmp_path: Path) -> None:
    frontend = tmp_path / "terminal-ui"
    entry = _write_terminal_ui_entry(frontend)

    cmd = _build_terminal_ui_command(
        "config.yaml",
        frontend_dir=frontend,
        node_executable="/opt/bin/node",
        bridge_python_executable="/venv/bin/python",
    )

    assert cmd[:4] == ["/opt/bin/node", str(entry), "--config", "config.yaml"]
    assert cmd[4] == "--bridge-command-json"
    assert json.loads(cmd[5]) == [
        "/venv/bin/python",
        "-m",
        "naumi_agent.ui.bridge",
        "--config",
        "config.yaml",
    ]


@pytest.mark.parametrize(
    ("version", "major"),
    [
        ("v20.11.1", 20),
        ("22.3.0", 22),
        ("node-v20", None),
        ("", None),
    ],
)
def test_parse_node_major(version: str, major: int | None) -> None:
    assert _parse_node_major(version) == major


def test_build_terminal_ui_command_rejects_old_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "terminal-ui"
    _write_terminal_ui_entry(frontend)
    monkeypatch.setattr(
        "naumi_agent.main.subprocess.check_output",
        lambda *args, **kwargs: "v19.9.0\n",
    )

    with pytest.raises(TerminalUiLaunchError, match="需要 Node.js 20\\+"):
        _build_terminal_ui_command(
            "config.yaml",
            frontend_dir=frontend,
            node_executable="/opt/bin/node",
        )


def test_build_terminal_ui_command_rejects_unparseable_node_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "terminal-ui"
    _write_terminal_ui_entry(frontend)
    monkeypatch.setattr(
        "naumi_agent.main.subprocess.check_output",
        lambda *args, **kwargs: "not-node\n",
    )

    with pytest.raises(TerminalUiLaunchError, match="无法识别 Node.js 版本输出"):
        _build_terminal_ui_command(
            "config.yaml",
            frontend_dir=frontend,
            node_executable="/opt/bin/node",
        )


def test_resolve_terminal_ui_frontend_prefers_source_tree(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    package_root = tmp_path / "package"
    source_frontend = project_root / "frontend" / "terminal-ui"
    package_frontend = package_root / "frontend" / "terminal-ui"
    _write_terminal_ui_entry(source_frontend)
    _write_terminal_ui_entry(package_frontend)

    resolved = _resolve_terminal_ui_frontend_dir(
        project_root=project_root,
        package_root=package_root,
    )

    assert resolved == source_frontend


def test_build_terminal_ui_command_uses_packaged_frontend_when_source_is_missing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    package_root = tmp_path / "package"
    package_frontend = package_root / "frontend" / "terminal-ui"
    entry = _write_terminal_ui_entry(package_frontend)

    cmd = _build_terminal_ui_command(
        "config.yaml",
        project_root=project_root,
        package_root=package_root,
        node_executable="/opt/bin/node",
        bridge_python_executable="/venv/bin/python",
    )

    assert cmd[:4] == ["/opt/bin/node", str(entry), "--config", "config.yaml"]
    assert json.loads(cmd[5]) == [
        "/venv/bin/python",
        "-m",
        "naumi_agent.ui.bridge",
        "--config",
        "config.yaml",
    ]


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
    _write_terminal_ui_entry(frontend)
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


def test_ui_command_launches_next_terminal_ui_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_launch_terminal_ui(config_path: str) -> int:
        calls.append(config_path)
        return 3

    monkeypatch.setattr("naumi_agent.main._launch_terminal_ui", fake_launch_terminal_ui)

    result = runner.invoke(naumi_app, ["ui", "--config", "custom.yaml"])

    assert result.exit_code == 3
    assert calls == ["custom.yaml"]


def test_ui_command_legacy_flag_uses_old_textual_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config_path: calls.append(config_path),
    )
    monkeypatch.setattr(
        "naumi_agent.main._launch_terminal_ui",
        lambda config_path: pytest.fail("legacy flag must not launch terminal-ui"),
    )

    result = runner.invoke(naumi_app, ["ui", "--legacy", "--config", "legacy.yaml"])

    assert result.exit_code == 0
    assert calls == ["legacy.yaml"]


def test_ui_command_reports_legacy_fallback_when_next_ui_cannot_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_launch_terminal_ui(config_path: str) -> int:
        raise TerminalUiLaunchError("未找到 Node.js")

    monkeypatch.setattr("naumi_agent.main._launch_terminal_ui", fake_launch_terminal_ui)

    result = runner.invoke(naumi_app, ["ui"])

    assert result.exit_code == 1
    assert "未找到 Node.js" in result.output
    assert "naumi ui --legacy" in result.output
    assert "naumi chat --tui" in result.output


def test_terminal_ui_runtime_assets_are_included_in_wheel() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    force_include = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert force_include["frontend/terminal-ui/package.json"] == (
        "naumi_agent/frontend/terminal-ui/package.json"
    )
    assert force_include["frontend/terminal-ui/protocol-contract.json"] == (
        "naumi_agent/frontend/terminal-ui/protocol-contract.json"
    )
    assert force_include["frontend/terminal-ui/src"] == (
        "naumi_agent/frontend/terminal-ui/src"
    )
    assert "frontend/terminal-ui/test" not in force_include
