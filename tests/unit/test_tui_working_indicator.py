"""Focused tests for the Textual working animation frame."""

from naumi_agent.tui.working_indicator import (
    WORKING_INDICATOR_FRAME_COUNT,
    render_working_indicator_frame,
)


def test_working_indicator_frames_are_stable_semantic_rich_text() -> None:
    frames = [
        render_working_indicator_frame(index)
        for index in range(WORKING_INDICATOR_FRAME_COUNT)
    ]

    assert len({frame.plain for frame in frames}) == 4
    assert len({len(frame.plain) for frame in frames}) == 1
    assert all("Naumi 工作中" in frame.plain for frame in frames)
    assert {"cyan", "bold magenta", "bold green"} <= {
        str(span.style) for span in frames[0].spans
    }
    assert render_working_indicator_frame(-1).plain == frames[3].plain
    assert render_working_indicator_frame(1003).plain == frames[3].plain
