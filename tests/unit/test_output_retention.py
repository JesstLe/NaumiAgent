import json
import os
import time
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.tools.base import ToolResult
from naumi_agent.tools.output_publish import asset_root, publish_bytes
from naumi_agent.tools.output_retention import (
    OutputAssetRetention,
    OutputAssetRetentionPreviewTool,
    OutputAssetRetentionRunTool,
)


@pytest.fixture
def retention_context(tmp_path):
    session_db = tmp_path / "sessions.db"
    config = AppConfig(memory=MemoryConfig(session_db_path=str(session_db)))
    chat_store = ChatRunStore(tmp_path / "chat-runs.db")
    engine = SimpleNamespace(config=config, chat_run_store=chat_store)
    retention = OutputAssetRetention(
        directory=asset_root(config),
        session_db_path=session_db,
        chat_run_db_path=chat_store.db_path,
    )
    return engine, retention


def _publish(config: AppConfig, content: bytes, suffix: str = ".txt") -> tuple[str, str]:
    url = publish_bytes(config, content, suffix)
    return url, url.rsplit("/", 1)[-1]


def _make_old(path, *, seconds: int = 7200) -> None:
    timestamp = time.time() - seconds
    os.utime(path, (timestamp, timestamp))


async def test_retention_keeps_session_reference_and_deletes_real_orphan(
    retention_context,
):
    engine, retention = retention_context
    referenced_url, referenced_name = _publish(engine.config, b"referenced")
    _, orphan_name = _publish(engine.config, b"orphan")
    directory = asset_root(engine.config)
    _make_old(directory / referenced_name)
    _make_old(directory / orphan_name)

    store = SessionStore(engine.config.memory)
    await store.save(
        Session(
            id="kept-session",
            messages=[{"role": "assistant", "content": f"[报告]({referenced_url})"}],
        )
    )

    preview = retention.preview(minimum_age_seconds=60)
    assert preview.scan_complete
    assert preview.referenced_assets == 1
    assert [item.name for item in preview.orphan_candidates] == [orphan_name]

    result = retention.run(minimum_age_seconds=60, delete_limit=10)
    assert result.deleted_names == (orphan_name,)
    assert (directory / referenced_name).is_file()
    assert not (directory / orphan_name).exists()
    await store.close()


async def test_archived_and_shared_session_references_remain_protected(
    retention_context,
):
    engine, retention = retention_context
    url, name = _publish(engine.config, b"shared")
    path = asset_root(engine.config) / name
    _make_old(path)
    store = SessionStore(engine.config.memory)
    for session_id in ("first", "second"):
        await store.save(
            Session(
                id=session_id,
                messages=[{"role": "assistant", "content": f"![共享]({url})"}],
            )
        )
    await store.archive("first")
    await store.delete("second")

    result = retention.run(minimum_age_seconds=60)
    assert result.deleted_names == ()
    assert result.preview.referenced_assets == 1
    assert path.is_file()
    await store.close()


async def test_run_step_reference_protects_asset_before_final_message(
    retention_context,
):
    engine, retention = retention_context
    url, name = _publish(engine.config, b"tool-result")
    path = asset_root(engine.config) / name
    _make_old(path)
    run = await engine.chat_run_store.start_run(
        session_id="session-a",
        user_message_id="message-a",
    )
    await engine.chat_run_store.append_step(
        run.id,
        sequence=1,
        stage="tool",
        status="completed",
        summary="生成与返回内容",
        detail=json.dumps({"url": url}),
    )

    preview = retention.preview(minimum_age_seconds=60)
    assert preview.referenced_assets == 1
    assert preview.orphan_candidates == ()
    assert path.is_file()


def test_recent_unmanaged_and_unsafe_entries_are_never_deleted(retention_context):
    engine, retention = retention_context
    _, recent_name = _publish(engine.config, b"recent")
    directory = asset_root(engine.config)
    (directory / "operator-note.txt").write_text("keep", encoding="utf-8")
    unsafe_name = "f" * 64 + ".txt"
    (directory / unsafe_name).mkdir()

    preview = retention.preview(minimum_age_seconds=3600)
    assert preview.deferred_recent == 1
    assert preview.unmanaged_entries == 1
    assert preview.unsafe_entries == 1
    assert preview.orphan_candidates == ()
    result = retention.run(minimum_age_seconds=3600)
    assert result.deleted_names == ()
    assert (directory / recent_name).is_file()
    assert (directory / "operator-note.txt").is_file()
    assert (directory / unsafe_name).is_dir()


def test_cleanup_is_bounded_and_refuses_incomplete_reference_scan(
    retention_context,
    monkeypatch,
):
    engine, retention = retention_context
    directory = asset_root(engine.config)
    names = []
    for index in range(3):
        _, name = _publish(engine.config, f"orphan-{index}".encode())
        _make_old(directory / name)
        names.append(name)

    first = retention.run(minimum_age_seconds=60, delete_limit=2)
    assert len(first.deleted_names) == 2
    assert sum((directory / name).is_file() for name in names) == 1

    monkeypatch.setattr(
        "naumi_agent.tools.output_retention.scan_persisted_output_references",
        lambda *args, **kwargs: (set(), 50_000, False),
    )
    blocked = retention.run(minimum_age_seconds=60, delete_limit=2)
    assert blocked.deleted_names == ()
    assert blocked.errors == (
        "引用或资源扫描达到上限，为避免误删，本轮未执行清理。",
    )
    assert sum((directory / name).is_file() for name in names) == 1


def test_directory_scan_limit_fails_closed(retention_context, monkeypatch):
    engine, retention = retention_context
    directory = asset_root(engine.config)
    for index in range(2):
        _, name = _publish(engine.config, f"bounded-{index}".encode())
        _make_old(directory / name)
    monkeypatch.setattr(
        "naumi_agent.tools.output_retention.MAX_DIRECTORY_ENTRIES",
        1,
    )

    result = retention.run(minimum_age_seconds=60)
    assert not result.preview.scan_complete
    assert result.deleted_names == ()
    assert len(list(directory.glob("*.txt"))) == 2


def test_changed_candidate_is_rechecked_before_delete(retention_context, monkeypatch):
    engine, retention = retention_context
    _, name = _publish(engine.config, b"changes-during-scan")
    path = asset_root(engine.config) / name
    _make_old(path)
    original_preview = retention._preview_locked

    def preview_then_change(*, minimum_age_seconds, now):
        preview = original_preview(
            minimum_age_seconds=minimum_age_seconds,
            now=now,
        )
        os.utime(path, None)
        return preview

    monkeypatch.setattr(retention, "_preview_locked", preview_then_change)
    result = retention.run(minimum_age_seconds=60)
    assert result.deleted_names == ()
    assert result.skipped_changed == 1
    assert path.is_file()


def test_republishing_existing_content_renews_cleanup_grace(retention_context):
    engine, retention = retention_context
    _, name = _publish(engine.config, b"same-content")
    path = asset_root(engine.config) / name
    _make_old(path)
    old_mtime = path.stat().st_mtime

    _publish(engine.config, b"same-content")
    assert path.stat().st_mtime > old_mtime
    preview = retention.preview(minimum_age_seconds=3600)
    assert preview.deferred_recent == 1
    assert preview.orphan_candidates == ()


@pytest.mark.parametrize("minimum_age", [0, 59, 604801, True, "3600"])
def test_retention_rejects_invalid_age_values(retention_context, minimum_age):
    _, retention = retention_context
    with pytest.raises(ValueError, match="60 到 604800"):
        retention.preview(minimum_age_seconds=minimum_age)


@pytest.mark.parametrize("delete_limit", [0, 501, True, "10"])
def test_retention_rejects_invalid_delete_limits(retention_context, delete_limit):
    _, retention = retention_context
    with pytest.raises(ValueError, match="1 到 500"):
        retention.run(minimum_age_seconds=60, delete_limit=delete_limit)


async def test_tools_and_shared_output_command_use_same_retention_logic(
    retention_context,
):
    engine, _ = retention_context
    preview_tool = OutputAssetRetentionPreviewTool(engine)
    run_tool = OutputAssetRetentionRunTool(engine)
    assert preview_tool.metadata.read_only
    assert run_tool.metadata.destructive
    assert run_tool.metadata.requires_confirmation

    async def execute_tool(call, **kwargs):
        tool = {
            preview_tool.name: preview_tool,
            run_tool.name: run_tool,
        }[call.name]
        return ToolResult(
            call_id=call.id,
            status="success",
            content=await tool.execute(**json.loads(call.arguments)),
        )

    engine.execute_tool = execute_tool
    preview = await execute_slash_command(engine, "/output retention-preview 60")
    assert "输出资源清理预览" in preview
    result = await execute_slash_command(engine, "/output retention-run 60 10")
    assert "输出资源清理结果" in result
    usage = await execute_slash_command(engine, "/output retention-run bad")
    assert "用法：/output retention-run" in strip_ansi(usage)


def test_engine_registers_output_asset_retention_tools(tmp_path):
    config = AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
    )
    engine = AgentEngine(config)
    assert engine.tool_registry.get("output_asset_retention_preview") is not None
    assert engine.tool_registry.get("output_asset_retention_run") is not None


def test_corrupt_reference_database_blocks_deletion(retention_context):
    engine, retention = retention_context
    _, name = _publish(engine.config, b"must-not-delete")
    path = asset_root(engine.config) / name
    _make_old(path)
    engine.chat_run_store.db_path.write_bytes(b"not a sqlite database")

    result = retention.run(minimum_age_seconds=60)
    assert result.deleted_names == ()
    assert not result.preview.scan_complete
    assert path.is_file()
