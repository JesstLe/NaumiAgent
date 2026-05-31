"""CLI layout behavior tests."""

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

from naumi_agent.cli.layout import CLIApp, _border_line


def _build_cli_app() -> CLIApp:
    cli = CLIApp()
    app = cli._build_app()
    assert app.mouse_support()
    assert cli._output_win is not None
    return cli


class TestCLIAppScrolling:
    def test_output_cursor_is_pinned_to_latest_line(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()
                for i in range(30):
                    cli.append_output(f"第 {i} 行\n")

                window = cli._output_win
                assert window is not None
                content = window.content.create_content(width=80, height=5)

                assert content.cursor_position.y == content.line_count - 1

                window._scroll(content, width=80, height=5)

                assert window.vertical_scroll == content.line_count - 5

    def test_wrapped_live_output_scrolls_within_long_line(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()
                cli.append_live("回复" * 80)

                window = cli._output_win
                assert window is not None
                content = window.content.create_content(width=10, height=3)

                assert content.cursor_position.x > 0

                window._scroll(content, width=10, height=3)

                assert window.vertical_scroll_2 > 0

    def test_manual_scroll_is_not_forced_back_to_bottom(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()
                for i in range(30):
                    cli.append_output(f"第 {i} 行\n")

                window = cli._output_win
                assert window is not None

                content = window.content.create_content(width=80, height=5)
                window._scroll(content, width=80, height=5)
                assert window.vertical_scroll == content.line_count - 5

                window._scroll_up()
                assert window.auto_scroll is False
                manual_scroll = window.vertical_scroll

                content = window.content.create_content(width=80, height=5)
                window._scroll(content, width=80, height=5)

                assert window.vertical_scroll == manual_scroll

    def test_manual_scroll_moves_inside_wrapped_line(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()
                cli.append_live("回复" * 80)

                window = cli._output_win
                assert window is not None

                content = window.content.create_content(width=10, height=3)
                window._scroll(content, width=10, height=3)
                bottom_offset = window.vertical_scroll_2
                assert bottom_offset > 0

                window._scroll_up()
                assert window.auto_scroll is False

                content = window.content.create_content(width=10, height=3)
                window._scroll(content, width=10, height=3)

                assert window.vertical_scroll_2 == bottom_offset - 1

    def test_finalize_live_keeps_auto_scroll_enabled(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()
                cli.append_live("思考过程\n" * 20)
                cli.finalize_live()

                window = cli._output_win
                assert window is not None
                assert window.auto_scroll is True
                assert window.vertical_scroll > 0

    def test_manual_scroll_keys_are_safe_before_first_render_info(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()

                window = cli._output_win
                assert window is not None

                window._scroll_up()
                assert window.auto_scroll is False

                window._scroll_down()
                assert window.vertical_scroll == 1

    def test_status_bar_is_not_part_of_transcript(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()
                cli.append_output("hello\n")
                cli.set_status("model | workspace | token")

                assert "hello" in cli.get_transcript()
                assert "model | workspace | token" not in cli.get_transcript()
                assert cli._render_status(80).__pt_formatted_text__()[0][1].strip().startswith(
                    "model"
                )

    def test_output_window_has_scrollbar_margin(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()

                window = cli._output_win
                assert window is not None
                assert window.right_margins

    def test_status_bar_uses_render_width(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()
                cli.set_status("abcdefghijklmnopqrstuvwxyz")

                small = cli._render_status(10).__pt_formatted_text__()[0][1]
                large = cli._render_status(30).__pt_formatted_text__()[0][1]

                assert len(small) == 10
                assert small.endswith("…")
                assert large.strip() == "abcdefghijklmnopqrstuvwxyz"

    def test_status_bar_truncates_wide_text_to_render_width(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()
                cli.set_status("上下文: 263K/256K | 预算: $0.5008/$5.00 | 📂 main*")

                rendered = cli._render_status(24).__pt_formatted_text__()[0][1]

                assert get_cwidth(rendered) == 24
                assert rendered.endswith("…")

    def test_reset_output_replaces_transcript_and_scroll_state(self) -> None:
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                cli = _build_cli_app()
                cli.append_output("old\n")
                assert "old" in cli.get_transcript()

                cli.reset_output()
                cli.append_output("new\n")

                assert cli.get_transcript() == "new\n"
                assert cli._output_win is not None
                assert cli._output_win.auto_scroll is True

    def test_border_line_uses_exact_render_width(self) -> None:
        assert _border_line(1, "╭", "─", "╮")[0][1] == "╭"
        assert _border_line(2, "╭", "─", "╮")[0][1] == "╭╮"
        assert _border_line(10, "╭", "─", "╮")[0][1] == "╭────────╮"
