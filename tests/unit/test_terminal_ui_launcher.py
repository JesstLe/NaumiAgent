from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from naumi_agent.config.paths import DEFAULT_CONFIG_PATH
from naumi_agent.config.settings import AppConfig
from naumi_agent.main import (
    TerminalUiLaunchError,
    _build_terminal_ui_command,
    _launch_interactive_ui,
    _launch_terminal_ui,
    _launch_tui,
    _parse_node_major,
    _resolve_terminal_ui_frontend_dir,
    naumiagent_app,
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
    monkeypatch.setattr("naumi_agent.main._ensure_onboarding_ready", lambda _config: None)


def _write_terminal_ui_entry(frontend: Path) -> Path:
    entry = frontend / "src" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    return entry


def test_textual_fallback_binds_engine_to_process_launch_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    launch = tmp_path / "launch"
    legacy.mkdir()
    launch.mkdir()
    config = AppConfig(
        models={"api_key": "test-secret"},
        workspace_root=str(legacy),
        safety={"allowed_dirs": [str(legacy)]},
    )
    captured: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, runtime_config):
            captured["config"] = runtime_config
            self.workspace_root = runtime_config.resolve_workspace_root()
            self.router = SimpleNamespace(resolve_model=lambda _tier: "test-model")

    class FakeTrace:
        def output(self, *_args, **_kwargs) -> None:
            return None

    class FakeApp:
        def __init__(self, engine, **_kwargs):
            captured["engine"] = engine

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.chdir(launch)
    monkeypatch.setattr(
        "naumi_agent.main.AppConfig.from_yaml",
        lambda _path: config,
    )
    monkeypatch.setattr("naumi_agent.orchestrator.engine.AgentEngine", FakeEngine)
    monkeypatch.setattr("naumi_agent.tui.app.NaumiApp", FakeApp)
    monkeypatch.setattr(
        "naumi_agent.debug_trace.DebugTrace.create",
        lambda **_kwargs: FakeTrace(),
    )
    monkeypatch.setattr("naumi_agent.log_setup.setup_logging", lambda _level: None)

    _launch_tui(str(tmp_path / "config.yaml"))

    assert config.workspace_root == str(launch.resolve())
    assert captured["engine"].workspace_root == launch.resolve()
    assert captured["ran"] is True


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


@pytest.mark.parametrize("returncode", [0, 130, 143])
def test_interactive_launcher_does_not_fallback_for_terminal_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    tui_calls: list[str] = []
    monkeypatch.setattr(
        "naumi_agent.main._launch_terminal_ui",
        lambda _config: returncode,
    )
    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config: tui_calls.append(config),
    )

    assert _launch_interactive_ui("project.yaml") == returncode
    assert tui_calls == []


@pytest.mark.parametrize(
    "failure",
    [TerminalUiLaunchError("未找到 Node.js"), OSError("spawn failed")],
)
def test_interactive_launcher_falls_back_once_for_launch_errors(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    tui_calls: list[str] = []

    def fail_terminal(_config: str) -> int:
        raise failure

    monkeypatch.setattr("naumi_agent.main._launch_terminal_ui", fail_terminal)
    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config: tui_calls.append(config),
    )

    assert _launch_interactive_ui("project.yaml") == 0
    assert tui_calls == ["project.yaml"]


def test_interactive_launcher_falls_back_once_for_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui_calls: list[str] = []
    monkeypatch.setattr("naumi_agent.main._launch_terminal_ui", lambda _config: 7)
    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config: tui_calls.append(config),
    )

    assert _launch_interactive_ui("project.yaml") == 0
    assert tui_calls == ["project.yaml"]


def test_interactive_launcher_reports_tui_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_calls = 0
    tui_calls = 0
    output: list[str] = []

    def fail_terminal(_config: str) -> int:
        nonlocal terminal_calls
        terminal_calls += 1
        raise TerminalUiLaunchError("missing terminal assets")

    def fail_tui(_config: str) -> None:
        nonlocal tui_calls
        tui_calls += 1
        raise RuntimeError("tui failed with sk-abcdefghijklmnopqrstuvwxyz")

    monkeypatch.setattr("naumi_agent.main._launch_terminal_ui", fail_terminal)
    monkeypatch.setattr("naumi_agent.main._launch_tui", fail_tui)
    monkeypatch.setattr(
        "naumi_agent.main.console.print",
        lambda message: output.append(str(message)),
    )

    assert _launch_interactive_ui("project.yaml") == 1
    assert terminal_calls == 1
    assert tui_calls == 1
    assert "正在切换到 Textual TUI" in "\n".join(output)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in "\n".join(output)


def test_naumi_without_subcommand_launches_terminal_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "naumi_agent.main._launch_interactive_ui",
        lambda config_path: calls.append(config_path) or 0,
    )
    monkeypatch.setattr(
        "naumi_agent.main._chat",
        lambda _config: pytest.fail("default entry must not launch classic chat"),
    )

    result = runner.invoke(naumi_app, [])

    assert result.exit_code == 0
    assert calls == [DEFAULT_CONFIG_PATH]


def test_chat_command_launches_terminal_ui_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "naumi_agent.main._launch_interactive_ui",
        lambda config_path: calls.append(config_path) or 0,
    )

    result = runner.invoke(naumi_app, ["chat", "--config", "custom.yaml"])

    assert result.exit_code == 0
    assert calls == ["custom.yaml"]


def test_classic_prompt_toolkit_option_is_not_registered() -> None:
    root_result = runner.invoke(naumi_app, ["--classic"])
    chat_result = runner.invoke(naumi_app, ["chat", "--classic"])

    assert root_result.exit_code != 0
    assert chat_result.exit_code != 0
    assert "No such option" in root_result.output
    assert "No such option" in chat_result.output


@pytest.mark.parametrize(
    "args",
    [
        ["--tui", "--config", "root.yaml"],
        ["chat", "--tui", "--config", "chat.yaml"],
        ["tui", "--config", "tui.yaml"],
    ],
)
def test_explicit_tui_entries_bypass_terminal_ui(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    tui_calls: list[str] = []
    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config: tui_calls.append(config),
    )
    monkeypatch.setattr(
        "naumi_agent.main._launch_interactive_ui",
        lambda _config: pytest.fail("explicit TUI must bypass Node UI"),
    )

    result = runner.invoke(naumi_app, args)

    assert result.exit_code == 0
    assert tui_calls == [args[-1]]


def test_ui_command_launches_next_terminal_ui_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_launch_interactive_ui(config_path: str) -> int:
        calls.append(config_path)
        return 3

    monkeypatch.setattr(
        "naumi_agent.main._launch_interactive_ui",
        fake_launch_interactive_ui,
    )

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
        "naumi_agent.main._launch_interactive_ui",
        lambda config_path: pytest.fail("legacy flag must not launch terminal-ui"),
    )

    result = runner.invoke(naumi_app, ["ui", "--legacy", "--config", "legacy.yaml"])

    assert result.exit_code == 0
    assert calls == ["legacy.yaml"]
    assert "--legacy" in result.output
    assert "naumi tui" in result.output


def test_naumiagent_defaults_to_terminal_ui_and_tui_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_calls: list[str] = []
    tui_calls: list[str] = []
    monkeypatch.setattr(
        "naumi_agent.main._launch_interactive_ui",
        lambda config: terminal_calls.append(config) or 0,
    )
    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config: tui_calls.append(config),
    )

    default_result = runner.invoke(naumiagent_app, [])
    tui_result = runner.invoke(
        naumiagent_app,
        ["--tui", "--config", "custom.yaml"],
    )

    assert default_result.exit_code == 0
    assert tui_result.exit_code == 0
    assert terminal_calls == [DEFAULT_CONFIG_PATH]
    assert tui_calls == ["custom.yaml"]


def test_naumiagent_console_script_is_registered() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert data["project"]["scripts"]["naumiagent"] == (
        "naumi_agent.main:naumiagent_cli"
    )


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


def test_help_suppresses_optional_litellm_provider_warnings() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.pop("NAUMI_SHOW_STARTUP_WARNINGS", None)
    env["PYTHONPATH"] = str(repo_root / "src")

    result = subprocess.run(
        [sys.executable, "-m", "naumi_agent.main", "--help"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    assert "could not pre-load" not in result.stderr
    assert "Bedrock event-stream" not in result.stderr
