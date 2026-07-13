"""TUI 组件测试."""

import asyncio
import logging

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine, AgentRuntimeMode
from naumi_agent.tui.app import (
    ActivityPanel,
    ChatPanel,
    InputBar,
    NaumiApp,
    PermissionConfirmScreen,
    StatusBar,
    TodoBar,
    _build_textual_bindings,
    _capture_tui_terminal_noise,
    _captured_terminal_text,
    _find_latest_user_session_id,
    _format_tool_output_markdown,
)
from naumi_agent.tui.completion_receipt import format_completion_receipt_markdown
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


class TestNaumiApp:
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
        assert "`src/example.py`" in rendered
        assert "pytest tests/unit/test_example.py -q" in rendered
        assert "bash_run · 仅本次允许" in rendered
        assert "风险：1 项验证失败" in rendered
        assert "下一步：重试失败验证" in rendered
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
