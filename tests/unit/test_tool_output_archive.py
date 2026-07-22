from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tools.base import ToolResult
from naumi_agent.ui.tool_output_archive import (
    ToolOutputArchive,
    ToolOutputArchiveError,
    render_tool_output_page,
)


def test_archive_pages_unicode_and_verifies_session_and_digest(tmp_path) -> None:
    archive = ToolOutputArchive(tmp_path / "outputs", page_chars=1024)
    content = ("第一行 abc\n" * 300) + "结尾"
    reference = archive.archive(
        content,
        session_id="session-a",
        tool_call_id="call-1",
    )

    assert reference.page_count > 1
    restored = "".join(
        archive.read_page(reference.artifact_id, page, session_id="session-a").content
        for page in range(1, reference.page_count + 1)
    )
    assert restored == content
    assert reference.content_chars == len(content)
    assert reference.content_bytes == len(content.encode())
    with pytest.raises(ToolOutputArchiveError, match="不属于当前会话"):
        archive.read_page(reference.artifact_id, 1, session_id="session-b")
    with pytest.raises(ToolOutputArchiveError, match="页码"):
        archive.read_page(reference.artifact_id, reference.page_count + 1, session_id="session-a")

    page_path = tmp_path / "outputs" / reference.artifact_id / "000001.txt"
    page_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ToolOutputArchiveError, match="摘要不一致"):
        archive.read_page(reference.artifact_id, 1, session_id="session-a")


def test_archive_rejects_corrupt_manifest_and_renders_safe_fence(tmp_path) -> None:
    archive = ToolOutputArchive(tmp_path / "outputs", page_chars=1024)
    reference = archive.archive(
        "before\n~~~~\nafter",
        session_id="session-a",
        tool_call_id="call-1",
    )
    page = archive.read_page(reference.artifact_id, 1, session_id="session-a")
    rendered = render_tool_output_page(page)
    assert "~~~~~text" in rendered
    assert f"/tool-output {reference.artifact_id} 1" in rendered

    manifest_path = tmp_path / "outputs" / reference.artifact_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ToolOutputArchiveError, match="版本不兼容"):
        archive.read_page(reference.artifact_id, 1, session_id="session-a")


@pytest.mark.asyncio
async def test_shared_slash_command_reads_only_active_session_page(tmp_path) -> None:
    archive = ToolOutputArchive(tmp_path / "outputs", page_chars=1024)
    reference = archive.archive(
        "page content",
        session_id="session-a",
        tool_call_id="call-1",
    )
    engine = SimpleNamespace(
        _session=SimpleNamespace(id="session-a"),
        tool_output_archive=archive,
    )

    output = strip_ansi(
        await execute_slash_command(
            engine,
            f"/tool-output {reference.artifact_id} 1",
        )
    )
    assert "第 1/1 页" in output
    assert "page content" in output

    engine._session.id = "session-b"
    denied = strip_ansi(
        await execute_slash_command(
            engine,
            f"/tool-output {reference.artifact_id} 1",
        )
    )
    assert "不属于当前会话" in denied


def test_engine_archives_only_oversized_tool_results(tmp_path) -> None:
    engine = object.__new__(AgentEngine)
    engine.tool_output_archive = ToolOutputArchive(tmp_path / "outputs")

    small = engine._archive_tool_output_event_fields(
        ToolResult(call_id="small", status="success", content="ok"),
        session_id="session-a",
    )
    large_content = "中" * 3_000
    large = engine._archive_tool_output_event_fields(
        ToolResult(call_id="large", status="success", content=large_content),
        session_id="session-a",
    )

    assert small == {}
    assert large["content_length"] == len(large_content)
    assert large["content_bytes"] == len(large_content.encode())
    page = engine.tool_output_archive.read_page(
        large["output_artifact_id"],
        1,
        session_id="session-a",
    )
    assert page.content == large_content


def test_archive_deletes_only_exact_session_without_following_symlinks(tmp_path) -> None:
    archive = ToolOutputArchive(tmp_path / "outputs", page_chars=1024)
    first = archive.archive("first", session_id="session-a", tool_call_id="a")
    second = archive.archive("second", session_id="session-b", tool_call_id="b")
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = archive.root / ("out_" + "f" * 32)
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except OSError:
        symlink = None

    assert archive.delete_session("session-a") == 1
    with pytest.raises(ToolOutputArchiveError, match="未找到"):
        archive.read_page(first.artifact_id, 1, session_id="session-a")
    assert archive.read_page(second.artifact_id, 1, session_id="session-b").content == "second"
    assert outside.exists()
    if symlink is not None:
        assert symlink.is_symlink()
