from __future__ import annotations

import json

import pytest

from naumi_agent.daemons.agent_worker_tool_rpc import (
    decode_tool_call_batch,
    decode_tool_manifest,
    decode_tool_result_batch,
    encode_tool_call_batch,
    encode_tool_manifest,
    encode_tool_result_batch,
    issue_tool_call_batch,
    issue_tool_manifest,
    issue_tool_result_batch,
)
from naumi_agent.tools.base import ToolResult


def _schema(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"执行 {name}",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }


def test_tool_rpc_contracts_round_trip_with_exact_order() -> None:
    manifest = issue_tool_manifest(
        tool_scope=("alpha", "beta"),
        tools=[_schema("alpha"), _schema("beta")],
    )
    assert decode_tool_manifest(encode_tool_manifest(manifest)) == manifest

    calls = issue_tool_call_batch(
        execution_id="agent-model-execution",
        turn=2,
        raw_calls=[
            {
                "id": "call-alpha",
                "type": "function",
                "function": {"name": "alpha", "arguments": '{"value":"证据"}'},
            },
            {
                "id": "call-beta",
                "type": "function",
                "function": {"name": "beta", "arguments": '{"value":"继续"}'},
            },
        ],
        tool_scope=manifest.tool_scope,
    )
    assert decode_tool_call_batch(encode_tool_call_batch(calls)) == calls

    results = issue_tool_result_batch(
        call_batch=calls,
        results=[
            ToolResult("call-alpha", "success", "alpha 完成", 3),
            ToolResult("call-beta", "error", "beta 被权限拒绝", 0),
        ],
    )
    assert decode_tool_result_batch(encode_tool_result_batch(results)) == results


def test_tool_manifest_requires_exact_sorted_scope_and_schema_fields() -> None:
    with pytest.raises(ValueError, match="排序"):
        issue_tool_manifest(
            tool_scope=("beta", "alpha"),
            tools=[_schema("beta"), _schema("alpha")],
        )
    with pytest.raises(ValueError, match="精确"):
        issue_tool_manifest(
            tool_scope=("alpha",),
            tools=[_schema("beta")],
        )
    invalid = _schema("alpha")
    invalid["unexpected"] = True
    with pytest.raises(ValueError, match="顶层字段"):
        issue_tool_manifest(tool_scope=("alpha",), tools=[invalid])

    mutable = issue_tool_manifest(
        tool_scope=("alpha",),
        tools=[_schema("alpha")],
    )
    mutable.tools[0]["function"]["description"] = "tampered"
    with pytest.raises(ValueError, match="摘要"):
        encode_tool_manifest(mutable)


def test_tool_call_rejects_out_of_scope_invalid_json_and_duplicate_ids() -> None:
    raw = {
        "id": "call-alpha",
        "type": "function",
        "function": {"name": "alpha", "arguments": "{}"},
    }
    with pytest.raises(ValueError, match="scope 外"):
        issue_tool_call_batch(
            execution_id="agent-model-execution",
            turn=1,
            raw_calls=[raw],
            tool_scope=("beta",),
        )
    invalid = json.loads(json.dumps(raw))
    invalid["function"]["arguments"] = "[]"
    with pytest.raises(ValueError, match="JSON object"):
        issue_tool_call_batch(
            execution_id="agent-model-execution",
            turn=1,
            raw_calls=[invalid],
            tool_scope=("alpha",),
        )
    with pytest.raises(ValueError, match="不能重复"):
        issue_tool_call_batch(
            execution_id="agent-model-execution",
            turn=1,
            raw_calls=[raw, raw],
            tool_scope=("alpha",),
        )
    duplicate_content = json.loads(json.dumps(raw))
    duplicate_content["id"] = "call-alpha-other"
    duplicate_content["function"]["arguments"] = '{ "value" : 1 }'
    first_content = json.loads(json.dumps(raw))
    first_content["function"]["arguments"] = '{"value":1}'
    with pytest.raises(ValueError, match="内容不能重复"):
        issue_tool_call_batch(
            execution_id="agent-model-execution",
            turn=1,
            raw_calls=[first_content, duplicate_content],
            tool_scope=("alpha",),
        )


def test_tool_result_rejects_reordered_or_unknown_status() -> None:
    calls = issue_tool_call_batch(
        execution_id="agent-model-execution",
        turn=1,
        raw_calls=[
            {
                "id": "call-alpha",
                "type": "function",
                "function": {"name": "alpha", "arguments": "{}"},
            }
        ],
        tool_scope=("alpha",),
    )
    with pytest.raises(ValueError, match="call_id"):
        issue_tool_result_batch(
            call_batch=calls,
            results=[ToolResult("call-other", "success", "", 0)],
        )
    with pytest.raises(ValueError, match="status"):
        issue_tool_result_batch(
            call_batch=calls,
            results=[ToolResult("call-alpha", "unknown", "", 0)],
        )


def test_tool_rpc_decoder_rejects_unknown_fields() -> None:
    manifest = issue_tool_manifest(
        tool_scope=("alpha",),
        tools=[_schema("alpha")],
    )
    payload = json.loads(encode_tool_manifest(manifest))
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="字段集合"):
        decode_tool_manifest(json.dumps(payload).encode("utf-8"))
