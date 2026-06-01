"""Shared analysis infrastructure tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis_common import (
    read_sources,
    resolve_target,
    router_unavailable,
    run_analysis,
)


def test_resolve_target_returns_source_files_from_directory(tmp_path: Path) -> None:
    py_file = tmp_path / "app.py"
    txt_file = tmp_path / "notes.txt"
    py_file.write_text("print('ok')\n", encoding="utf-8")
    txt_file.write_text("ignore\n", encoding="utf-8")

    files = resolve_target(str(tmp_path))

    assert files == [py_file]


def test_read_sources_annotates_files(tmp_path: Path) -> None:
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("a = 1\n", encoding="utf-8")
    second.write_text("b = '" + ("x" * 500) + "'\n", encoding="utf-8")

    output = read_sources([first, second], max_chars=1000)

    assert f"### {first}" in output
    assert "a = 1" in output
    assert f"### {second}" in output


@pytest.mark.asyncio
async def test_run_analysis_calls_router_with_capable_tier() -> None:
    response = ModelResponse(
        content="分析完成",
        usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        model="test",
    )
    router = AsyncMock()
    router.call = AsyncMock(return_value=response)

    output = await run_analysis(router, "system", "user")

    assert output == "分析完成"
    router.call.assert_awaited_once()
    assert router.call.await_args.kwargs["max_tokens"] == 16384


def test_router_unavailable_is_user_facing() -> None:
    output = router_unavailable("entropy", "目标" * 200)

    assert "Router 未注入" in output
    assert "模式: entropy" in output
    assert len(output) < 400
