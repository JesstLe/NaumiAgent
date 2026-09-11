"""Unit tests for the pi RPC JSONL client using a fake pi subprocess.

The fake speaks the real wire protocol: ``response`` records echo the
command id, events stream without envelopes, and ``extension_ui_response``
records are consumed without any response.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from naumi_agent.pi_engine.rpc import PiRpcClient, PiRpcError

FAKE_PI_SCRIPT = textwrap.dedent(
    """
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        sys.stdout.flush()

    prompt_seen = []
    ui_responses = []

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            # Mirror the real RPC entry: report a parse error and keep going.
            send({"type": "response", "id": None, "command": "parse",
                  "success": False, "error": f"Failed to parse command: {exc}"})
            continue
        kind = record.get("type")
        if kind == "extension_ui_response":
            ui_responses.append(record)
            continue
        cid = record.get("id")
        if kind == "get_state":
            send({"type": "response", "id": cid, "command": "get_state",
                  "success": True,
                  "data": {"sessionId": "s-1", "model": {"id": "glm-4.7",
                  "provider": "zai-coding-cn"}}})
        elif kind == "prompt":
            prompt_seen.append(record.get("message"))
            send({"type": "response", "id": cid, "command": "prompt",
                  "success": True})
            send({"type": "agent_start"})
            send({"type": "message_update", "assistantMessageEvent":
                  {"type": "text_delta", "contentIndex": 0, "delta": "ok"}})
            send({"type": "agent_settled"})
        elif kind == "set_model":
            send({"type": "response", "id": cid, "command": "set_model",
                  "success": False, "error": "Model not found: x/y"})
        elif kind == "abort":
            send({"type": "response", "id": cid, "command": "abort",
                  "success": True})
        elif kind == "dump_ui_responses":
            send({"type": "response", "id": cid, "command": "dump_ui_responses",
                  "success": True, "data": {"responses": ui_responses}})
    """
)


@pytest.fixture
async def fake_pi(tmp_path):
    script = tmp_path / "fake_pi.py"
    script.write_text(FAKE_PI_SCRIPT, encoding="utf-8")
    client = await PiRpcClient.start(argv=[sys.executable, str(script)])
    yield client
    await client.stop()


async def test_request_response_correlation(fake_pi: PiRpcClient) -> None:
    state = await fake_pi.get_state()
    assert state["sessionId"] == "s-1"
    assert state["model"]["id"] == "glm-4.7"


async def test_failed_command_raises_chinese_error(fake_pi: PiRpcClient) -> None:
    with pytest.raises(PiRpcError, match="set_model"):
        await fake_pi.set_model("x", "y")


async def test_events_stream_through_handler(fake_pi: PiRpcClient) -> None:
    received: list[dict] = []
    gate = asyncio.Event()

    original = fake_pi._event_handler

    def handler(record: dict) -> None:
        received.append(record)
        if record.get("type") == "agent_settled":
            gate.set()
        if original is not None:
            original(record)

    fake_pi._event_handler = handler
    await fake_pi.prompt("hi")
    await asyncio.wait_for(gate.wait(), timeout=5)
    kinds = [r["type"] for r in received]
    assert kinds[0] == "agent_start"
    assert "message_update" in kinds
    assert kinds[-1] == "agent_settled"


async def test_extension_ui_response_uses_top_level_fields(
    fake_pi: PiRpcClient,
) -> None:
    # pi's dialog parser reads confirmed/value/cancelled at the TOP level of
    # the record, so the client must not nest them under a result key.
    await fake_pi.send_extension_ui_response("req-1", {"confirmed": True})
    await fake_pi.send_extension_ui_response("req-2", {"cancelled": True})
    data = await fake_pi.request("dump_ui_responses")
    responses = data["responses"]
    assert responses[0] == {
        "type": "extension_ui_response",
        "id": "req-1",
        "confirmed": True,
    }
    assert responses[1] == {
        "type": "extension_ui_response",
        "id": "req-2",
        "cancelled": True,
    }


async def test_malformed_json_line_is_survived(fake_pi: PiRpcClient) -> None:
    assert fake_pi._process.stdin is not None
    fake_pi._process.stdin.write(b"this is not json\n")
    await fake_pi._process.stdin.drain()
    await asyncio.sleep(0.2)
    state = await fake_pi.get_state()
    assert state["sessionId"] == "s-1"
