"""Tests for shared bottom-bar layout helpers."""

from __future__ import annotations

from naumi_agent.ui.layout import (
    BottomBarState,
    build_full_status_text,
    clip_to_width,
    compute_output_guard_height,
    format_activity_bar,
    format_status_bar,
    format_todo_bar,
)


class TestBottomBarState:
    def test_immutable(self) -> None:
        state = BottomBarState(mode="bypass")
        assert state.mode == "bypass"
        try:
            state.mode = "plan"
            raise AssertionError("Should be frozen")
        except AttributeError:
            pass


class TestFormatStatusBar:
    def test_default_mode(self) -> None:
        state = BottomBarState(mode="default", status="就绪")
        text = format_status_bar(state)
        assert "mode: default" in text
        assert "就绪" in text

    def test_plan_mode(self) -> None:
        state = BottomBarState(mode="plan", status="只读模式")
        text = format_status_bar(state)
        assert "mode: plan" in text

    def test_empty_status(self) -> None:
        state = BottomBarState(mode="default", status="")
        text = format_status_bar(state)
        assert "mode: default" in text


class TestFormatTodoBar:
    def test_with_todo(self) -> None:
        state = BottomBarState(todo_text="todo: 2/5 完成 | ● #3 编写测试")
        text = format_todo_bar(state)
        assert "编写测试" in text

    def test_no_todo(self) -> None:
        state = BottomBarState()
        text = format_todo_bar(state)
        assert text == ""


class TestFormatActivityBar:
    def test_with_activity(self) -> None:
        state = BottomBarState(activity_text="准备 file_write · test.py")
        text = format_activity_bar(state)
        assert "file_write" in text

    def test_no_activity(self) -> None:
        state = BottomBarState()
        text = format_activity_bar(state)
        assert text == ""


class TestBuildFullStatusText:
    def test_full_status(self) -> None:
        text = build_full_status_text(
            model="gpt-4o",
            workspace="/tmp/project",
            tokens="Token: 1234",
            budget="$0.01/$10.00",
            git_branch="main",
            git_dirty=True,
        )
        assert "gpt-4o" in text
        assert "工作区" in text
        assert "main*" in text

    def test_minimal_status(self) -> None:
        text = build_full_status_text()
        assert text == ""


class TestClipToWidth:
    def test_short_text_padded(self) -> None:
        result = clip_to_width("hi", 10)
        assert len(result) == 10

    def test_exact_fit(self) -> None:
        result = clip_to_width("hello", 5)
        assert result == "hello"

    def test_long_text_truncated(self) -> None:
        result = clip_to_width("hello world this is long", 10)
        assert result.endswith("…")
        # The ellipsis takes 1 cell, so 9 chars + … = 10 cells
        assert len(result) <= 11  # May vary by emoji width handling

    def test_zero_width(self) -> None:
        result = clip_to_width("anything", 0)
        assert result == ""

    def test_single_char_width(self) -> None:
        result = clip_to_width("abc", 1)
        assert len(result) <= 1


class TestComputeOutputGuardHeight:
    def test_base_height_no_extras(self) -> None:
        # status(1) + border_top(1) + input(1) + border_bot(1) = 4
        height = compute_output_guard_height()
        assert height == 4

    def test_with_todo(self) -> None:
        height = compute_output_guard_height(has_todo=True)
        assert height == 5

    def test_with_activity(self) -> None:
        height = compute_output_guard_height(has_activity=True)
        assert height == 5

    def test_with_both(self) -> None:
        height = compute_output_guard_height(has_todo=True, has_activity=True)
        assert height == 6
