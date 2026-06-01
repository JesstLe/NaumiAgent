"""Tests for virtualized CLI transcript history."""

from __future__ import annotations

from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.utils import get_cwidth

from naumi_agent.cli.history import VirtualizedCLIHistory, VirtualizedHistoryControl
from naumi_agent.cli.renderers import CLIRenderer
from naumi_agent.ui.messages.replay import replay_messages


class TestVirtualizedCLIHistory:
    def test_append_live_finalize_and_clear(self) -> None:
        history = VirtualizedCLIHistory()

        history.append_output("old\n")
        history.append_live("live\n")

        assert history.transcript() == "old\nlive\n"
        assert history.stats().output_chunks == 1
        assert history.stats().live_chunks == 1

        assert history.finalize_live() == 1
        assert history.stats().output_chunks == 2
        assert history.stats().live_chunks == 0

        history.clear()
        assert history.transcript() == ""
        assert history.line_count() == 1

    def test_visible_text_reads_bottom_window_with_offset(self) -> None:
        history = VirtualizedCLIHistory()
        for i in range(20):
            history.append_output(f"第 {i} 行\n")

        bottom = history.visible_text(width=80, height=3)
        older = history.visible_text(width=80, height=3, scroll_offset=5)

        assert "第 19 行" in bottom
        assert "第 17 行" in bottom
        assert "第 14 行" in older
        assert "第 19 行" not in older

    def test_formats_only_requested_lines_and_invalidates_on_resize(self) -> None:
        history = VirtualizedCLIHistory()
        history.append_output("\033[32m绿色\033[0m\n普通\n")

        first_80 = history.get_line(0, width=80)
        first_80_again = history.get_line(0, width=80)
        first_40 = history.get_line(0, width=40)

        assert first_80 is first_80_again
        assert first_40 is not first_80
        assert history.stats().cached_lines == 2
        assert fragment_list_to_text(first_80) == "绿色"

        history.append_output("新增\n")
        assert history.stats().cached_lines == 0

    def test_control_pins_cursor_to_last_line_when_auto_scroll_enabled(self) -> None:
        history = VirtualizedCLIHistory()
        history.append_output("第一行\n第二行")
        control = VirtualizedHistoryControl(history, should_pin_cursor=lambda: True)

        content = control.create_content(width=80, height=5)

        assert content.cursor_position.y == 1
        assert content.cursor_position.x == get_cwidth("第二行")
        assert any(fragment[0] == "[SetCursorPosition]" for fragment in content.get_line(1))

    def test_replayed_session_can_be_stored_as_virtualized_history(self) -> None:
        renderer = CLIRenderer()
        history = VirtualizedCLIHistory()
        raw_messages = [
            {"role": "user", "content": "读取 README"},
            {
                "role": "assistant",
                "content": "我先查看文件。",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "function": {
                            "name": "file_read",
                            "arguments": '{"file_path": "README.md"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "# NaumiAgent"},
            {"role": "assistant", "content": "README 是项目说明。"},
        ]

        for message in replay_messages(raw_messages):
            rendered = renderer.render(message)
            if rendered:
                history.append_output(rendered)

        viewport = history.visible_text(width=100, height=12)
        assert "读取 README" in viewport
        assert "file_read" in viewport
        assert "README 是项目说明" in viewport
        assert history.line_count() < 80
