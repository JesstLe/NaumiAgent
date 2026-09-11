"""Public summaries describe observable actions without model reasoning."""

import json

import pytest

from naumi_agent.api.routes.messages import _persist_stream_event
from naumi_agent.runs.public_activity import progress_summary, tool_action
from naumi_agent.runs.recorder import ChatRunRecorder
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType
from naumi_agent.streaming.events import runtime_event_to_stream_event


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        (
            "bash_run",
            {"cwd": "E:/Workspace/NaumiAgent", "command": "pnpm build"},
            "在 E:/Workspace/NaumiAgent 执行命令：pnpm build",
        ),
        (
            "file_edit",
            {"path": "src/app.py", "new_string": "PRIVATE_CONTENT"},
            "修改文件：src/app.py",
        ),
        ("browser_observe", {}, "查看页面元素与布局"),
        ("browser_evaluate", {"code": "PRIVATE_CODE"}, "在页面执行检查脚本"),
        ("read", '{"path":"README.md"}', "读取文件：README.md"),
        ("unknown", "{broken", "调用工具：unknown"),
        ("read", [], "读取文件"),
    ],
)
def test_action_uses_operation_targets_only(name, args, expected):
    assert tool_action({"name": name, "args": args, "thinking": "PRIVATE_REASONING"}) == expected


def test_summary_redacts_credentials_and_omits_url_query():
    result = tool_action(
        {"name": "bash_run", "args": {"command": "curl -H 'Bearer abc123' token=short"}}
    )
    assert "abc123" not in result and "short" not in result
    result = tool_action(
        {
            "name": "web_fetch",
            "args": {"url": "https://user:pass@example.com/page?key=secret#token"},
        }
    )
    assert result == "访问页面：https://example.com/page"
    result = tool_action(
        {
            "name": "bash_run",
            "args": {
                "command": (
                    'curl -H "Authorization: Bearer abc123" token="short" password=\'quoted\''
                )
            },
        }
    )
    assert all(value not in result for value in ("abc123", "short", "quoted"))


def test_progress_requires_facts_and_uses_message_counts():
    assert progress_summary("thinking_delta", {"content": "PRIVATE_REASONING"}) == ""
    assert (
        progress_summary(
            "context_compacted", {"before": 120, "after": 30, "archived_tool_results": 2}
        )
        == "已压缩上下文：120 → 30 条消息；归档 2 条工具结果"
    )
    assert progress_summary("context_compacted", {"before": -1, "after": True}) == "已压缩上下文"
    assert (
        progress_summary(
            "task_snapshot",
            {"items": [{"status": "in_progress", "subject": "修复归档接口"}], "completed_count": 1},
        )
        == "执行计划 · 进行中：修复归档接口；已完成 1 项"
    )
    assert (
        progress_summary(
            "phase_summary",
            {
                "items": [
                    {"action": "读取文件：README.md", "status": "success"},
                    {"action": "执行命令：pnpm build", "status": "error"},
                ]
            },
        )
        == "本阶段执行存在 1 项失败：读取文件：README.md；执行命令：pnpm build。"
    )
    assert (
        progress_summary(
            "phase_summary",
            {
                "phase_kind": "recovery",
                "items": [
                    {"action": "检测到模型未执行写入，已继续调用工具", "status": "completed"}
                ],
            },
        )
        == "执行恢复：检测到模型未执行写入，已继续调用工具。"
    )
    assert progress_summary("phase_summary", {"items": [{"status": "success"}]}) == ""


def test_harness_correction_becomes_visible_phase_summary():
    runtime = RuntimeEvent(
        id="correction-1",
        type=RuntimeEventType.HARNESS_COMPLETION_CORRECTION,
        data={"message": "动作型请求尚未产生工作区文件变更，已继续执行。"},
        timestamp="2026-09-11T00:00:00Z",
        session_id="s",
        run_id="r",
        sequence=2,
        turn=1,
    )

    transport = runtime_event_to_stream_event(runtime)

    assert transport.type.value == "phase_summary"
    assert transport.data["items"][0]["action"] == (
        "动作型请求尚未产生工作区文件变更，已继续执行。"
    )
    assert "activity_summary" in transport.data


@pytest.mark.asyncio
async def test_public_summary_survives_transport_both_recorders_and_reopen(tmp_path):
    store = ChatRunStore(tmp_path / "runs.db")
    recorder = await ChatRunRecorder.start(
        store=store, workspace_root=tmp_path, session_id="s", task="检查布局"
    )
    http_run = await store.start_run(session_id="s", user_message_id="http")
    sequences = {}
    events = [
        (RuntimeEventType.THINKING_DELTA, {"content": "PRIVATE_REASONING"}),
        (
            RuntimeEventType.TOOL_START,
            {"name": "file_read", "call_id": "a", "args": {"path": "README.md"}},
        ),
        (
            RuntimeEventType.PERMISSION_BUBBLE,
            {"tool_name": "file_read", "call_id": "a", "status": "allowed"},
        ),
        (
            RuntimeEventType.TOOL_END,
            {"name": "file_read", "call_id": "a", "status": "success", "content": "文件内容"},
        ),
        (
            RuntimeEventType.CONTEXT_COMPACTED,
            {"before": 100, "after": 25, "archived_tool_results": 1},
        ),
        (
            RuntimeEventType.TASK_SNAPSHOT,
            {"items": [{"status": "pending", "subject": "检查 UI 布局"}], "completed_count": 0},
        ),
        (
            RuntimeEventType.PHASE_SUMMARY,
            {"items": [{"action": "读取文件：README.md", "status": "success"}]},
        ),
    ]
    for index, (kind, data) in enumerate(events):
        runtime = RuntimeEvent(
            id=f"e{index}",
            type=kind,
            data=data,
            timestamp="2026-09-11T00:00:00Z",
            session_id="s",
            run_id=recorder.run_id,
            sequence=index + 1,
            turn=1,
        )
        transport = runtime_event_to_stream_event(runtime)
        assert "PRIVATE_REASONING" not in json.dumps(transport.to_dict())
        await recorder.observe(kind.value, {**data, "event_id": runtime.id, "turn": 1})
        await _persist_stream_event(store, http_run.id, transport, sequences)
    reopened = ChatRunStore(tmp_path / "runs.db")
    for run_id in (recorder.run_id, http_run.id):
        run = await reopened.get_run("s", run_id)
        assert run is not None
        tool = next(step for step in run.steps if step.stage == "tool")
        assert tool.metadata["public_action"] == "读取文件：README.md"
        assert tool.detail == "文件内容"
        activity = [step.summary for step in run.steps if step.stage == "activity"]
        assert activity == [
            "已压缩上下文：100 → 25 条消息；归档 1 条工具结果",
            "执行计划 · 待处理：检查 UI 布局；已完成 0 项",
            "本阶段已完成：读取文件：README.md。",
        ]
        assert all("PRIVATE_REASONING" not in step.detail for step in run.steps)
