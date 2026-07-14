"""Phase 7 integration tests — engine browser/security integration and slash commands."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine


class TestEngineTaskRunnerProperty:
    def test_task_runner_lazy_init(self) -> None:
        engine = AgentEngine(AppConfig())
        assert engine._task_runner is None

    def test_task_runner_creates_on_access(self) -> None:
        engine = AgentEngine(AppConfig(browser={
            "max_concurrent_runs": 3,
            "run_history_limit": 80,
        }))
        runner = engine.task_runner
        assert runner is not None
        assert engine._task_runner is not None
        assert runner.max_concurrent_runs == 3
        assert runner._run_history_limit == 80

    def test_task_runner_reuses(self) -> None:
        engine = AgentEngine(AppConfig())
        r1 = engine.task_runner
        r2 = engine.task_runner
        assert r1 is r2


class TestEngineSecurityAuditorProperty:
    def test_security_auditor_creates(self) -> None:
        engine = AgentEngine(AppConfig())
        auditor = engine.security_auditor
        assert auditor is not None


class TestEngineShutdownWithTaskRunner:
    @pytest.mark.asyncio
    async def test_shutdown_aborts_running_tasks(self) -> None:
        engine = AgentEngine(AppConfig())
        runner = engine.task_runner
        runner.runs = [
            {"id": "r1", "status": "running"},
            {"id": "r2", "status": "completed"},
            {"id": "r3", "status": "queued"},
        ]
        await engine.shutdown()
        assert runner.get_run("r1")["status"] == "aborting"
        assert runner.get_run("r2")["status"] == "completed"
        assert runner.get_run("r3")["status"] == "aborted"


class TestSlashCommandRouting:
    """Test that new slash commands are properly routed in _handle_command."""

    @pytest.mark.asyncio
    async def test_browse_no_arg(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        await _handle_command(engine, "/browse")
        # Should not crash, just print usage

    @pytest.mark.asyncio
    async def test_browser_stop(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        engine._browser_session = MagicMock()
        engine._browser_session.stop = AsyncMock(return_value={})
        await _handle_command(engine, "/browser-stop")
        engine._browser_session.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_browser_state(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        engine._browser_session = MagicMock()
        engine._browser_session.get_debug_state = MagicMock(
            return_value={"console": [], "network": []}
        )
        await _handle_command(engine, "/browser-state")
        engine._browser_session.get_debug_state.assert_called_once_with(20)

    @pytest.mark.asyncio
    async def test_tasks_list(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        runner = MagicMock()
        runner.list_runs.return_value = []
        engine.task_runner = runner
        await _handle_command(engine, "/tasks")
        runner.list_runs.assert_called_once_with(limit=20)

    @pytest.mark.asyncio
    async def test_task_detail_no_arg(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        await _handle_command(engine, "/task")
        # Should print usage

    @pytest.mark.asyncio
    async def test_task_abort(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        runner = MagicMock()
        runner.get_run.return_value = {"id": "abc", "status": "running"}
        engine.task_runner = runner
        await _handle_command(engine, "/task-abort abc")
        runner.abort_run.assert_called_once_with("abc", reason="User requested")

    @pytest.mark.asyncio
    async def test_scan_no_arg(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        await _handle_command(engine, "/scan")
        # Should print usage

    @pytest.mark.asyncio
    async def test_scale_numeric_arg_routes_to_qps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import naumi_agent.main as main

        recorded: list[tuple[str, str, dict[str, object]]] = []

        async def fake_run_analysis(
            _: MagicMock, mode: str, target: str, **kwargs: object
        ) -> None:
            recorded.append((mode, target, kwargs))

        monkeypatch.setattr(main, "_run_analysis", fake_run_analysis)

        await main._handle_command(MagicMock(), "/scale 5000")

        assert recorded == [("scale", "当前项目", {"qps": 5000})]

    @pytest.mark.asyncio
    async def test_scan_full_no_arg(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        await _handle_command(engine, "/scan-full")
        # Should print usage

    @pytest.mark.asyncio
    async def test_scan_report_no_results(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        auditor = MagicMock()
        auditor.results = []
        engine.security_auditor = auditor
        await _handle_command(engine, "/scan-report")
        # Should print "no results" message

    @pytest.mark.asyncio
    async def test_btemplate_list_empty(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        runner = MagicMock()
        runner.list_templates.return_value = []
        engine.task_runner = runner
        await _handle_command(engine, "/btemplate-list")
        runner.list_templates.assert_called_once()

    @pytest.mark.asyncio
    async def test_btemplate_run_no_arg(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        await _handle_command(engine, "/btemplate-run")
        # Should print usage

    @pytest.mark.asyncio
    async def test_btemplate_compare_no_arg(self) -> None:
        from naumi_agent.main import _handle_command

        engine = MagicMock()
        await _handle_command(engine, "/btemplate-compare")
        # Should print usage

    @pytest.mark.asyncio
    async def test_tool_slash_commands_dispatch_to_tool_execution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from naumi_agent.main import _handle_command
        from naumi_agent.tools.base import ToolResult

        engine = MagicMock()
        engine.tool_registry = {
            "glob": MagicMock(),
            "file_read": MagicMock(),
            "file_write": MagicMock(),
        }
        engine._execute_tool = AsyncMock(
            return_value=ToolResult(
                call_id="call-1", status="success", content="ok"
            )
        )
        engine._execute_tool.__name__ = "_execute_tool"  # for compatibility

        await _handle_command(engine, '/glob "src/**/*.py" "."')
        await _handle_command(engine, "/read src/main.py")
        await _handle_command(engine, "/write src/tmp.txt hello")

        assert engine._execute_tool.await_count == 3
        tool_names = []
        args_payload = []
        for call in engine._execute_tool.await_args_list:
            tool_call = call.args[0]
            tool_names.append(tool_call.name)
            args_payload.append(json.loads(tool_call.arguments))
        assert tool_names == ["glob", "file_read", "file_write"]
        assert args_payload[0]["pattern"] == "src/**/*.py"
        assert args_payload[0]["directory"] == "."
        assert args_payload[1]["path"] == "src/main.py"
        assert args_payload[2]["path"] == "src/tmp.txt"


class TestSlashBatchParsing:
    @pytest.mark.asyncio
    async def test_execute_slash_command_splits_and_normalizes_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shlex

        import naumi_agent.main as main
        from naumi_agent.cli.slash_router import execute_slash_command

        recorded = []

        async def fake_handle_command(_: MagicMock, command: str) -> None:
            recorded.append(command)

        monkeypatch.setattr(main, "_handle_command", fake_handle_command)
        await execute_slash_command(
            MagicMock(),
            '/glob "src/**/*.py" .; /read src/main.py && /write src/todo.txt hi',
        )

        assert [shlex.split(cmd) for cmd in recorded] == [
            ["/glob", "src/**/*.py", "."],
            ["/read", "src/main.py"],
            ["/write", "src/todo.txt", "hi"],
        ]

    @pytest.mark.asyncio
    async def test_execute_slash_command_preserves_quoted_path_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shlex

        import naumi_agent.main as main
        from naumi_agent.cli.slash_router import execute_slash_command

        recorded = []

        async def fake_handle_command(_: MagicMock, command: str) -> None:
            recorded.append(command)

        monkeypatch.setattr(main, "_handle_command", fake_handle_command)
        await execute_slash_command(MagicMock(), '/read "src files/main file.py"')

        assert shlex.split(recorded[0]) == ["/read", "src files/main file.py"]

    @pytest.mark.asyncio
    async def test_execute_slash_command_accepts_history_typo_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shlex

        import naumi_agent.main as main
        from naumi_agent.cli.slash_router import execute_slash_command

        recorded = []

        async def fake_handle_command(_: MagicMock, command: str) -> None:
            recorded.append(command)

        monkeypatch.setattr(main, "_handle_command", fake_handle_command)
        await execute_slash_command(MagicMock(), "/histroy preview abc123")

        assert shlex.split(recorded[0]) == ["/history", "preview", "abc123"]


class TestCLICompleterCommands:
    """Tests requiring prompt_toolkit — skip if unavailable."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_prompt_toolkit(self) -> None:
        pytest.importorskip("prompt_toolkit")
    def test_browser_commands_in_list(self) -> None:
        from naumi_agent.cli_completer import COMMANDS

        cmd_names = [c[0] for c in COMMANDS]
        assert "/browse" in cmd_names
        assert "/autobrowse" in cmd_names
        assert "/browser-stop" in cmd_names
        assert "/browser-state" in cmd_names
        assert "/browser-screenshot" in cmd_names

    def test_task_commands_in_list(self) -> None:
        from naumi_agent.cli_completer import COMMANDS

        cmd_names = [c[0] for c in COMMANDS]
        assert "/diff" in cmd_names
        assert "/tasks" in cmd_names
        assert "/task" in cmd_names
        assert "/task-reply" in cmd_names
        assert "/task-abort" in cmd_names
        assert "/task-resume" in cmd_names

    def test_scan_commands_in_list(self) -> None:
        from naumi_agent.cli_completer import COMMANDS

        cmd_names = [c[0] for c in COMMANDS]
        assert "/scan" in cmd_names
        assert "/scan-full" in cmd_names
        assert "/scan-report" in cmd_names
        assert "/scan-baseline" in cmd_names

    def test_template_commands_in_list(self) -> None:
        from naumi_agent.cli_completer import COMMANDS

        cmd_names = [c[0] for c in COMMANDS]
        assert "/btemplate-list" in cmd_names
        assert "/btemplate-run" in cmd_names
        assert "/btemplate-compare" in cmd_names

    def test_new_commands_have_descriptions(self) -> None:
        from naumi_agent.cli_completer import COMMANDS

        new_cmds = {
            "/browse", "/autobrowse", "/browser-stop", "/browser-state",
            "/browser-screenshot", "/diff", "/tasks", "/task", "/task-reply",
            "/task-abort", "/task-resume", "/scan", "/scan-full",
            "/scan-report", "/scan-baseline", "/btemplate-list",
            "/btemplate-run", "/btemplate-compare",
        }
        for cmd, desc, takes_arg in COMMANDS:
            if cmd in new_cmds:
                assert desc, f"{cmd} has no description"
                assert isinstance(takes_arg, bool)
