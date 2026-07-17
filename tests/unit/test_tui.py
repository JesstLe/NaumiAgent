"""TUI 组件测试."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.widgets import Input

from naumi_agent.config.settings import AppConfig, ModelConfig, ModelMeta
from naumi_agent.harness.coordinator import ReconciliationCoordinatorOutcome
from naumi_agent.orchestrator.engine import AgentEngine, AgentRuntimeMode
from naumi_agent.tui.app import (
    ActivityPanel,
    ChatPanel,
    HistoryPanel,
    InputBar,
    NaumiApp,
    PermissionConfirmScreen,
    StatusBar,
    TodoBar,
    UserInteractionScreen,
    _build_textual_bindings,
    _capture_tui_terminal_noise,
    _captured_terminal_text,
    _find_latest_user_session_id,
    _format_tool_output_markdown,
)
from naumi_agent.tui.completion_receipt import (
    format_completion_receipt_markdown,
    format_completion_receipt_text,
)
from naumi_agent.tui.semantic_markdown import semantic_markdown_parser
from naumi_agent.ui.keybindings import build_keybindings
from naumi_agent.ui.theme import build_ui_style_config


class FakeMarkdown:
    def __init__(self) -> None:
        self.content = ""

    def update(self, content: str) -> None:
        self.content = content


class _FakeSession:
    def __init__(self, session_id: str, messages: list[dict[str, str]]) -> None:
        self.id = session_id
        self.messages = messages


class _PagedSessionEngine:
    def __init__(self, sessions: list[_FakeSession]) -> None:
        self.sessions = sessions

    async def list_sessions(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[_FakeSession], int]:
        start = (page - 1) * page_size
        end = start + page_size
        return self.sessions[start:end], len(self.sessions)


class _HistoryDispatchApp:
    def __init__(self) -> None:
        self.status = type("Status", (), {"status_text": ""})()
        self._show_session_delete_preview = MagicMock()
        self._show_session_retention_preview = MagicMock()
        self._run_session_retention = MagicMock()
        self._control_session_retention_worker = MagicMock()

    def query_one(self, widget_type: type[object]) -> object:
        if widget_type is StatusBar:
            return self.status
        return type("Widget", (), {})()


class TestNaumiApp:
    @pytest.mark.asyncio
    async def test_startup_recovery_starts_long_running_services_before_ready(
        self,
    ) -> None:
        recovery = SimpleNamespace(
            outcome=ReconciliationCoordinatorOutcome.COMPLETED
        )
        engine = SimpleNamespace(
            start_long_running_services=AsyncMock(return_value=(recovery,))
        )
        status = SimpleNamespace(status_text="")

        class _App:
            def __init__(self) -> None:
                self.engine = engine

            def query_one(self, widget_type: type[object]) -> object:
                assert widget_type is StatusBar
                return status

        await NaumiApp._recover_session_reconciliations.__wrapped__(_App())

        engine.start_long_running_services.assert_awaited_once_with()
        assert status.status_text == "会话协调恢复: 1/1 完成"

    @pytest.mark.asyncio
    async def test_startup_recovery_failure_is_sanitized_and_actionable(self) -> None:
        engine = SimpleNamespace(
            start_long_running_services=AsyncMock(
                side_effect=RuntimeError("secret database path")
            )
        )
        status = SimpleNamespace(status_text="")

        class _App:
            def __init__(self) -> None:
                self.engine = engine

            def query_one(self, widget_type: type[object]) -> object:
                assert widget_type is StatusBar
                return status

        await NaumiApp._recover_session_reconciliations.__wrapped__(_App())

        assert "secret database path" not in status.status_text
        assert status.status_text == (
            "会话协调恢复失败，周期清理未启动；请运行 /doctor 查看诊断"
        )

    @pytest.mark.asyncio
    async def test_delete_session_surfaces_durable_retry_and_clears_active_chat(
        self,
    ) -> None:
        session = SimpleNamespace(id="session-1")

        class _Engine:
            def __init__(self) -> None:
                self._session = session

            async def delete_session_detailed(self, session_id: str):
                assert session_id == "session-1"
                self._session = None
                return SimpleNamespace(
                    outcome=ReconciliationCoordinatorOutcome.RETRY_SCHEDULED,
                    request_id="delete-request-1",
                )

        status = SimpleNamespace(status_text="")
        chat = SimpleNamespace(clear=MagicMock())
        history = SimpleNamespace(show_panel=True, refresh_sessions=MagicMock())

        class _App:
            engine = _Engine()

            def query_one(self, widget_type: type[object]) -> object:
                return {
                    StatusBar: status,
                    ChatPanel: chat,
                    HistoryPanel: history,
                }[widget_type]

        await NaumiApp._delete_session.__wrapped__(
            _App(),
            "session-1",
            "重要会话",
        )

        assert status.status_text == "删除协调等待安全重试: delete-request-1"
        chat.clear.assert_called_once_with()
        history.refresh_sessions.assert_called_once_with()

    def test_history_delete_preview_dispatches_to_read_only_worker(self) -> None:
        app = _HistoryDispatchApp()

        NaumiApp._run_history_command(app, "delete-preview session-1")

        app._show_session_delete_preview.assert_called_once_with("session-1")

    def test_history_retention_preview_dispatches_to_read_only_worker(self) -> None:
        app = _HistoryDispatchApp()

        NaumiApp._run_history_command(app, "retention-preview")

        app._show_session_retention_preview.assert_called_once_with()

    def test_history_retention_run_dispatches_to_destructive_worker(self) -> None:
        app = _HistoryDispatchApp()

        NaumiApp._run_history_command(app, "retention-run")

        app._run_session_retention.assert_called_once_with()

    def test_history_retention_worker_dispatches_action(self) -> None:
        app = _HistoryDispatchApp()

        NaumiApp._run_history_command(app, "retention-worker wake")

        app._control_session_retention_worker.assert_called_once_with("wake")

    def test_semantic_markdown_parser_preserves_math_as_visible_content(self) -> None:
        tokens = semantic_markdown_parser().parse(
            "内联 $x^2$，未闭合 $y\n\n$$\ny=x+1\n$$"
        )
        inline = next(token for token in tokens if token.type == "inline")
        math = next(child for child in inline.children or [] if child.type == "code_inline")
        block = next(token for token in tokens if token.type == "fence")

        assert math.content == "$x^2$"
        assert any(
            child.type == "text" and "$y" in child.content
            for child in inline.children or []
        )
        assert block.info == "latex"
        assert "y=x+1" in block.content

    def test_completion_receipt_text_colors_git_statuses_semantically(self) -> None:
        rendered = format_completion_receipt_text(
            {
                "schema_version": 1,
                "receipt_id": "receipt-colors",
                "run_id": "run-colors",
                "outcome": "partial",
                "summary": "颜色检查。",
                "changes": [
                    {"path": f"src/{status}.py", "status": status}
                    for status in (
                        "added",
                        "deleted",
                        "modified",
                        "renamed",
                        "conflicted",
                        "restored",
                    )
                ],
                "validations": [],
                "unverified": [],
                "approvals": [],
                "risks": [],
                "git_state": {
                    "available": True,
                    "branch": "codex/colors",
                    "dirty": True,
                    "ahead": 1,
                    "behind": 2,
                },
                "next_actions": [],
                "duration_ms": 100,
            }
        )

        styles = {str(span.style) for span in rendered.spans}
        assert "新增 1 个文件" in rendered.plain
        assert "删除 1 个文件" in rendered.plain
        assert {"green", "red", "yellow", "cyan", "blue", "bold red"} <= styles

    def test_completion_receipt_markdown_matches_terminal_evidence(self) -> None:
        rendered = format_completion_receipt_markdown(
            {
                "schema_version": 1,
                "receipt_id": "receipt-tui",
                "run_id": "run-tui",
                "outcome": "partial",
                "summary": "修改已完成，但验证失败。",
                "changes": [
                    {
                        "path": "src/example.py",
                        "status": "modified",
                        "source_tool": "file_edit",
                        "additions": 5,
                        "deletions": 1,
                    }
                ],
                "validations": [
                    {
                        "command": "pytest tests/unit/test_example.py -q",
                        "scope": "tests/unit/test_example.py",
                        "status": "failed",
                        "exit_code": 1,
                        "passed": 2,
                        "failed": 1,
                    }
                ],
                "unverified": ["端到端场景尚未执行。"],
                "approvals": [
                    {
                        "call_id": "test-1",
                        "tool_name": "bash_run",
                        "decision": "allowed_once",
                    }
                ],
                "risks": [
                    {
                        "code": "validation_failed",
                        "level": "high",
                        "message": "1 项验证失败。",
                    }
                ],
                "git_state": {
                    "available": True,
                    "branch": "codex/receipt",
                    "dirty": True,
                },
                "next_actions": [
                    {
                        "id": "retry",
                        "label": "重试失败验证",
                        "kind": "retry_validation",
                    }
                ],
                "duration_ms": 1200,
            }
        )

        assert "完成回执 · 部分完成" in rendered
        assert "影响：修改 1 个文件" in rendered
        assert "pytest tests/unit/test_example.py -q" in rendered
        assert "bash_run" not in rendered
        assert "风险：1 项验证失败" in rendered
        assert "下一步：重试失败验证" in rendered

    def test_completed_delete_receipt_is_compact_and_task_focused(self) -> None:
        rendered = format_completion_receipt_markdown(
            {
                "schema_version": 1,
                "receipt_id": "receipt-delete",
                "run_id": "run-delete",
                "outcome": "completed",
                "summary": "已删除 `/workspace/test` 目录及其所有内容。",
                "changes": [
                    {
                        "path": f"test/file-{index}.txt",
                        "status": "removed_untracked",
                        "scope": "task",
                    }
                    for index in range(6)
                ]
                + [
                    {
                        "path": ".naumi/terminal-ui-debug.jsonl",
                        "status": "modified",
                        "scope": "background",
                    }
                ],
                "validations": [
                    {
                        "command": "路径已不存在: /workspace/test",
                        "scope": "文件系统",
                        "status": "passed",
                        "exit_code": 0,
                    }
                ],
                "unverified": [],
                "approvals": [
                    {
                        "call_id": "delete-1",
                        "tool_name": "bash_run",
                        "decision": "bypass",
                    }
                ],
                "risks": [],
                "git_state": {
                    "available": True,
                    "branch": "main",
                    "dirty": True,
                },
                "next_actions": [],
                "duration_ms": 6300,
            }
        )

        assert "完成回执 · 已完成" in rendered
        assert "验证通过 · `路径已不存在: /workspace/test`" in rendered
        assert "影响：删除 6 个文件" in rendered
        assert "工作区另有 1 项运行时变化" in rendered
        assert "验证 **1/1**" not in rendered
        assert "未记录验证命令" not in rendered
        assert "bash_run" not in rendered
        assert "test/file-0.txt" not in rendered

    def test_app_creation(self) -> None:
        config = AppConfig()
        engine = AgentEngine(config)
        app = NaumiApp(engine)
        assert app.engine is engine
        assert app.TITLE == "⬡ NaumiAgent"

    def test_bindings_exist(self) -> None:
        config = AppConfig()
        engine = AgentEngine(config)
        app = NaumiApp(engine)
        binding_keys = [b.key for b in app.BINDINGS]
        assert "ctrl+q" in binding_keys
        assert "tab" in binding_keys
        assert "shift+tab" in binding_keys
        assert "ctrl+l" in binding_keys
        assert "ctrl+g" in binding_keys

    def test_custom_bindings_are_generated_for_tui(self) -> None:
        bindings = build_keybindings(
            {
                "copy_transcript": "Ctrl+X",
                "mode_cycle": "F2",
                "toggle_activity": "Ctrl+A",
            }
        )
        textual_bindings = _build_textual_bindings(bindings)

        binding_pairs = {(binding.key, binding.action) for binding in textual_bindings}

        assert ("ctrl+x", "copy_transcript") in binding_pairs
        assert ("f2", "cycle_runtime_mode") in binding_pairs
        assert ("ctrl+a", "toggle_activity") in binding_pairs
        assert ("ctrl+y", "copy_transcript") not in binding_pairs

    def test_tui_uses_configured_theme_css(self) -> None:
        config = AppConfig()
        engine = AgentEngine(config)
        app = NaumiApp(engine, style_config=build_ui_style_config(theme="high_contrast"))

        assert app._style_config.theme.name.value == "high_contrast"
        assert "#00ff00" in app.CSS
        assert "StatusBar" in app.CSS

    def test_tui_does_not_keep_legacy_analysis_router(self) -> None:
        assert not hasattr(NaumiApp, "_run_analysis_mode")

    def test_tui_slash_completion_includes_local_agents_page(self) -> None:
        assert "/agents" in InputBar()._build_slash_candidates("agents")

    @pytest.mark.asyncio
    async def test_resume_helper_skips_empty_recent_sessions(self) -> None:
        engine = _PagedSessionEngine([
            _FakeSession("empty", [{"role": "system", "content": "prompt"}]),
            _FakeSession("real", [{"role": "user", "content": "继续"}]),
        ])

        session_id = await _find_latest_user_session_id(engine, page_size=1)

        assert session_id == "real"

    def test_tool_output_markdown_wraps_raw_diff(self) -> None:
        rendered = _format_tool_output_markdown("--- a\n+++ b\n@@\n-old\n+new")

        assert rendered.startswith("```diff")
        assert "-old" in rendered
        assert "+new" in rendered

    def test_tool_output_markdown_preserves_existing_fence(self) -> None:
        rendered = _format_tool_output_markdown("```python\nprint('ok')\n```")

        assert rendered == "```python\nprint('ok')\n```"

    def test_tui_agent_run_captures_stray_terminal_noise(self, capsys) -> None:
        logger = logging.getLogger("LiteLLM")
        previous = logger.level
        logger.setLevel(logging.INFO)
        try:
            with _capture_tui_terminal_noise() as (stdout_buf, stderr_buf):
                print("LiteLLM completion noise")
                logger.info("hidden client log")
            captured_text = _captured_terminal_text(stdout_buf, stderr_buf)
        finally:
            logger.setLevel(previous)

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        assert "LiteLLM completion noise" in captured_text
        assert logger.level == previous

    def test_chat_panel_excerpts_long_code_blocks_but_keeps_full_response(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat = ChatPanel()
        widget = FakeMarkdown()
        code = "\n".join(f"line_{idx}" for idx in range(1, 84))
        full = f"```python\n{code}\n```\n"
        monkeypatch.setattr(chat, "scroll_end", lambda animate=False: None)
        chat._response_widget = widget

        chat.add_response_token(full)

        assert chat._response_text == full
        assert "line_80" in widget.content
        assert "line_81" not in widget.content
        assert "已隐藏 3 行代码" in widget.content

    def test_chat_panel_reuses_prepare_widget_for_tool_start(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat = ChatPanel()
        mounted: list[object] = []
        updated: list[object] = []

        class FakeToolWidget:
            def update(self, content: object) -> None:
                updated.append(content)

        monkeypatch.setattr(chat, "mount", lambda widget: mounted.append(widget))
        monkeypatch.setattr(chat, "scroll_end", lambda animate=False: None)

        prepare_widget = FakeToolWidget()
        chat._current_tool_widget = prepare_widget
        chat.start_tool("📝 file_write showcase.html")

        assert chat._current_tool_widget is prepare_widget
        assert len(mounted) == 0
        assert len(updated) == 1

    @pytest.mark.asyncio
    async def test_permission_confirmation_modal_returns_choice(self) -> None:
        engine = AgentEngine(AppConfig())
        app = NaumiApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            task = asyncio.create_task(
                app.confirm_permission(
                    {
                        "tool_name": "code_execute",
                        "reason": "该工具需要用户确认。",
                        "arguments": {"code": "print('ok')"},
                        "risk_level": "high",
                        "permission_mode": "moderate",
                    }
                )
            )
            await pilot.pause(0.1)
            await pilot.press("ctrl+i")
            await pilot.pause(0.05)
            assert isinstance(app.screen, PermissionConfirmScreen)
            await pilot.click("#allow")
            choice = await asyncio.wait_for(task, timeout=2)

        assert choice == "allow"

    @pytest.mark.asyncio
    async def test_user_interaction_modal_supports_arrow_choice(self) -> None:
        engine = AgentEngine(AppConfig())
        app = NaumiApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            task = asyncio.create_task(engine.request_user_input({
                "header": "实现策略",
                "question": "请选择执行方案",
                "options": [
                    {"value": "safe", "label": "安全方案", "description": "保留兼容路径"},
                    {"value": "fast", "label": "快速方案", "description": "优先交付速度"},
                ],
                "allow_custom": True,
                "custom_label": "其他方案",
            }))
            await pilot.pause(0.1)
            assert isinstance(app.screen, UserInteractionScreen)
            await pilot.press("down", "enter")
            result = await asyncio.wait_for(task, timeout=2)

        assert result == {"kind": "option", "value": "fast"}

    @pytest.mark.asyncio
    async def test_user_interaction_modal_accepts_custom_text(self) -> None:
        engine = AgentEngine(AppConfig())
        app = NaumiApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            task = asyncio.create_task(engine.request_user_input({
                "header": "实现策略",
                "question": "请选择执行方案",
                "options": [
                    {"value": "safe", "label": "安全方案", "description": "保留兼容路径"},
                    {"value": "fast", "label": "快速方案", "description": "优先交付速度"},
                ],
                "allow_custom": True,
                "custom_label": "其他方案",
            }))
            await pilot.pause(0.1)
            await pilot.click("#interaction-custom")
            custom = app.screen.query_one("#interaction-custom-input", Input)
            custom.value = "仅当前工作区"
            custom.focus()
            await pilot.press("enter")
            result = await asyncio.wait_for(task, timeout=2)

        assert result == {"kind": "custom", "custom_text": "仅当前工作区"}

    @pytest.mark.asyncio
    async def test_shift_tab_cycles_runtime_mode_in_status_bar(self) -> None:
        engine = AgentEngine(AppConfig())
        app = NaumiApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("shift+tab")
            await pilot.pause(0.1)
            status = app.query_one(StatusBar)

            assert engine.runtime_mode == AgentRuntimeMode.PLAN
            assert status.mode_text == "plan"
            assert "mode: plan" in str(status.render())

    @pytest.mark.asyncio
    async def test_startup_status_renders_unlimited_budget(self) -> None:
        engine = AgentEngine(AppConfig())
        app = NaumiApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            status = app.query_one(StatusBar)
            rendered = str(status.render())

            assert "预算: 不限 · 已用 $0.0000" in rendered
            assert "/$0.00" not in rendered

    @pytest.mark.asyncio
    async def test_status_bar_distinguishes_reasoning_text_and_effort(self) -> None:
        engine = AgentEngine(
            AppConfig(
                models=ModelConfig(
                    default_model="openai/reasoner",
                    reasoning_effort="medium",
                    model_info={
                        "openai/reasoner": ModelMeta(
                            reasoning_efforts=("low", "medium", "high")
                        )
                    },
                )
            )
        )
        app = NaumiApp(engine, show_reasoning=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.1)
            rendered = str(app.query_one(StatusBar).render())

            assert "思考文本: off" in rendered
            assert "强度: medium" in rendered

    @pytest.mark.asyncio
    async def test_effort_slash_updates_persistent_tui_status(self) -> None:
        engine = AgentEngine(
            AppConfig(
                models=ModelConfig(
                    default_model="openai/reasoner",
                    model_info={
                        "openai/reasoner": ModelMeta(
                            reasoning_efforts=("low", "medium", "high")
                        )
                    },
                )
            )
        )
        app = NaumiApp(engine)

        async with app.run_test(size=(120, 30)) as pilot:
            app._handle_slash_command("/effort high")
            await pilot.pause(0.2)
            status = app.query_one(StatusBar)

            assert status.effort_text == "high"
            assert "强度: high" in str(status.render())

    @pytest.mark.asyncio
    async def test_reasoning_slash_updates_text_visibility_not_effort(self) -> None:
        engine = AgentEngine(AppConfig())
        app = NaumiApp(engine, show_reasoning=False)

        async with app.run_test(size=(120, 30)) as pilot:
            app._handle_slash_command("/reasoning on")
            await pilot.pause(0.1)
            status = app.query_one(StatusBar)

            assert app._show_reasoning is True
            assert status.reasoning_text == "on"
            assert status.effort_text == "auto"

    @pytest.mark.asyncio
    async def test_startup_status_renders_catalog_provider_protocol_and_upstream(
        self,
        tmp_path,
    ) -> None:
        catalog_path = tmp_path / "providers.json"
        catalog_path.write_text(
            json.dumps(
                {
                    "providers": {
                        "nvidia": {
                            "apiFormat": "openai_responses",
                            "baseURL": "https://integrate.api.nvidia.com/v1",
                            "models": {
                                "local-glm": {"upstreamId": "z-ai/glm4.7"},
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        engine = AgentEngine(
            AppConfig(
                models=ModelConfig(
                    provider="nvidia",
                    catalog_path=str(catalog_path),
                    default_model="local-glm",
                )
            )
        )
        app = NaumiApp(engine)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.1)
            rendered = str(app.query_one(StatusBar).render())

            assert "提供方: nvidia/OpenAI Responses" in rendered
            assert "模型: local-glm → z-ai/glm4.7" in rendered

    @pytest.mark.asyncio
    async def test_startup_status_keeps_model_when_runtime_identity_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = AgentEngine(AppConfig())

        def fail_identity(_model: str) -> None:
            raise ValueError("invalid catalog")

        monkeypatch.setattr(engine.router, "get_runtime_identity", fail_identity)
        app = NaumiApp(engine)

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            rendered = str(app.query_one(StatusBar).render())

            assert f"模型: {engine.router.resolve_model('capable')}" in rendered
            assert "工作区:" in rendered

    @pytest.mark.asyncio
    async def test_todo_bar_is_hidden_until_it_has_open_tasks(self) -> None:
        engine = AgentEngine(AppConfig())
        app = NaumiApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            todo = app.query_one(TodoBar)
            assert "hidden" in todo.classes

            todo.todo_text = "todo: 0/1 完成 | ● #1 正在实现"
            await pilot.pause(0.1)
            assert "hidden" not in todo.classes

            todo.todo_text = ""
            await pilot.pause(0.1)
            assert "hidden" in todo.classes

    @pytest.mark.asyncio
    async def test_clear_chat_removes_runtime_task_panels(self) -> None:
        engine = AgentEngine(AppConfig())
        app = NaumiApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            todo = app.query_one(TodoBar)
            activity = app.query_one(ActivityPanel)
            todo.todo_text = "todo: 0/1 完成 | ● #1 正在实现"
            activity.add_tool_log("subagent", {"task": "scan"}, "success", 10)
            await pilot.pause(0.1)

            app.action_clear_chat()
            await pilot.pause(0.1)

            assert todo.todo_text == ""
            assert "hidden" in todo.classes
            assert len(activity.children) == 0
