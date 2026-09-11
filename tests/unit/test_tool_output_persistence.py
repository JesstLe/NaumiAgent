from __future__ import annotations

from dataclasses import replace

import pytest

from naumi_agent.api.routes.messages import _persist_stream_event
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runs.recorder import ChatRunRecorder
from naumi_agent.runs.recovery import restore_tool_previews
from naumi_agent.runs.store import ChatRunRecord, ChatRunStepRecord, ChatRunStore
from naumi_agent.streaming.events import EventType, StreamEvent


@pytest.mark.asyncio
async def test_recorder_preserves_output_errors_inputs_and_terminal_steps(tmp_path):
    store = ChatRunStore(tmp_path / "runs.db")
    recorder = await ChatRunRecorder.start(
        store=store, workspace_root=tmp_path, session_id="s", task="读取"
    )
    await recorder.observe("thinking_start", {})
    await recorder.observe(
        "tool_start", {"call_id": "a", "name": "read", "args": {"path": "a.txt"}}
    )
    await recorder.observe("tool_start", {"call_id": "b", "name": "read"})
    secret = "sk-" + "x" * 25
    await recorder.observe(
        "tool_end",
        {
            "call_id": "b",
            "name": "read",
            "status": "success",
            "content": "第一行\n第二行\n" + secret,
        },
    )
    await recorder.observe("tool_error", {"call_id": "a", "name": "read", "message": "文件不存在"})
    await recorder.observe("tool_start", {"call_id": "c", "name": "slow"})
    await recorder.finish("completed", "结束")
    run = await ChatRunStore(tmp_path / "runs.db").get_run("s", recorder.run_id)
    assert run.steps[1].status == "completed"
    assert run.steps[2].detail == "文件不存在"
    assert run.steps[2].metadata["input"] == '{"path": "a.txt"}'
    assert run.steps[3].detail.startswith("第一行\n第二行\n")
    assert secret not in run.steps[3].detail
    assert run.steps[3].metadata["output_recorded"] is True
    assert run.steps[4].status == "interrupted"
    assert await store.get_run("other-session", recorder.run_id) is None


@pytest.mark.asyncio
async def test_recorder_preserves_reasoning_turn_order_without_private_content(tmp_path):
    store = ChatRunStore(tmp_path / "runs.db")
    recorder = await ChatRunRecorder.start(
        store=store, workspace_root=tmp_path, session_id="s", task="检查"
    )
    await recorder.observe("turn_start", {"turn": 1})
    await recorder.observe("thinking_start", {"turn": 1})
    await recorder.observe("thinking_delta", {"turn": 1, "content": "private"})
    await recorder.observe("thinking_end", {"turn": 1, "content": "private"})
    await recorder.observe(
        "tool_end", {"turn": 1, "call_id": "a", "name": "read", "status": "success"}
    )
    await recorder.observe("turn_start", {"turn": 2})
    await recorder.finish("completed", "结束")
    run = await store.get_run("s", recorder.run_id)
    assert [(step.stage, step.summary) for step in run.steps] == [
        ("request", "检查"),
        ("analysis", "第 1 轮分析"),
        ("tool", "read"),
        ("analysis", "第 2 轮分析"),
    ]
    assert all("private" not in step.detail for step in run.steps)


@pytest.mark.asyncio
async def test_http_fallback_records_empty_output_and_archive_reference(tmp_path):
    store = ChatRunStore(tmp_path / "runs.db")
    run = await store.start_run(session_id="s", user_message_id="u")
    sequences = {}
    for kind, data in [
        (EventType.TOOL_CALL_START, {"call_id": "a", "name": "read"}),
        (
            EventType.TOOL_CALL_END,
            {
                "call_id": "a",
                "name": "read",
                "status": "success",
                "content": "preview",
                "content_length": 9999,
                "output_artifact_id": "out_abc",
            },
        ),
        (
            EventType.TOOL_CALL_END,
            {"call_id": "b", "name": "empty", "status": "success", "content": ""},
        ),
        (EventType.TOOL_CALL_ERROR, {"call_id": "c", "name": "read", "content": "命令失败"}),
    ]:
        await _persist_stream_event(store, run.id, StreamEvent(type=kind, data=data), sequences)
    await store.finish_run(run.id, status="cancelled")
    restored = await ChatRunStore(tmp_path / "runs.db").get_run("s", run.id)
    assert restored.steps[0].detail == "preview"
    assert restored.steps[0].metadata["output_truncated"] is True
    assert restored.steps[0].metadata["output_artifact_id"] == "out_abc"
    assert restored.steps[1].detail == ""
    assert restored.steps[1].metadata["output_recorded"] is True
    assert restored.steps[2].detail == "命令失败"


def legacy_run():
    return ChatRunRecord(
        id="r",
        session_id="s",
        user_message_id="u",
        status="completed",
        started_at="",
        updated_at="",
        steps=[
            ChatRunStepRecord(1, "request", "completed", "继续"),
            ChatRunStepRecord(2, "tool", "completed", "read"),
        ],
        receipt=CompletionReceipt.from_dict(
            {
                "schema_version": 1,
                "receipt_id": "receipt",
                "run_id": "r",
                "outcome": "completed",
                "summary": "完成",
                "git_state": {"available": False, "dirty": False},
                "evidence_refs": ["run:r:approval:a"],
            }
        ),
    )


def legacy_messages(call_id="a", output="历史输出"):
    return [
        {"role": "user", "content": "继续"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": call_id, "function": {"name": "read", "arguments": '{"path":"a"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": output},
    ]


def test_recovery_requires_matching_receipt_and_unambiguous_messages():
    run = legacy_run()
    restored = restore_tool_previews(run, legacy_messages())
    assert restored.steps[1].detail == "历史输出"
    assert restored.steps[1].metadata["recovered_from"] == "session_tool_message"
    assert run.steps[1].detail == ""  # Reads do not rewrite historical records.
    assert restore_tool_previews(run, legacy_messages("other")).steps[1].detail == ""
    assert (
        restore_tool_previews(replace(run, receipt=None), legacy_messages()).steps[1].detail == ""
    )
    assert restore_tool_previews(run, legacy_messages() * 2).steps[1].detail == ""
    assert restore_tool_previews(run, legacy_messages()[:-1]).steps[1].detail == ""


def test_recovery_preserves_known_empty_output_and_existing_evidence():
    run = legacy_run()
    run.steps[1].metadata["output_recorded"] = True
    assert restore_tool_previews(run, legacy_messages()).steps[1].detail == ""
    run.steps[1].metadata = {}
    run.steps[1].detail = "已保存"
    assert restore_tool_previews(run, legacy_messages()).steps[1].detail == "已保存"
