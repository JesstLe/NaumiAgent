"""Real LiteLLM-to-Anthropic Messages loopback coverage."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from naumi_agent.config.settings import ModelConfig
from naumi_agent.model.catalog import parse_provider_catalog_json
from naumi_agent.model.router import ModelRouter


def _handler(requests: list[tuple[str, dict[str, str], dict[str, Any]]]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            requests.append((self.path, dict(self.headers), body))
            if body.get("stream"):
                self._send_stream(with_tool=bool(body.get("tools")))
                return
            if body.get("tools"):
                self._send_json({
                    "id": "msg_tool",
                    "type": "message",
                    "role": "assistant",
                    "model": "vendor/model-v2",
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "file_read",
                        "input": {"path": "README.md"},
                    }],
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 4},
                })
                return
            self._send_json({
                "id": "msg_text",
                "type": "message",
                "role": "assistant",
                "model": "vendor/model-v2",
                "content": [{"type": "text", "text": "loopback-ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 3},
            })

        def _send_json(self, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_stream(self, *, with_tool: bool) -> None:
            if with_tool:
                events = [
                    ("message_start", {
                        "type": "message_start",
                        "message": {
                            "id": "msg_tool_stream",
                            "type": "message",
                            "role": "assistant",
                            "model": "vendor/model-v2",
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 6, "output_tokens": 0},
                        },
                    }),
                    ("content_block_start", {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "toolu_stream",
                            "name": "file_read",
                            "input": {},
                        },
                    }),
                    ("content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": '{"path":',
                        },
                    }),
                    ("content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": '"README.md"}',
                        },
                    }),
                    ("content_block_stop", {
                        "type": "content_block_stop",
                        "index": 0,
                    }),
                    ("message_delta", {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                        "usage": {"output_tokens": 5},
                    }),
                    ("message_stop", {"type": "message_stop"}),
                ]
                self._send_events(events)
                return
            events = [
                ("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": "msg_stream",
                        "type": "message",
                        "role": "assistant",
                        "model": "vendor/model-v2",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 4, "output_tokens": 0},
                    },
                }),
                ("content_block_start", {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }),
                ("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "流式"},
                }),
                ("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "成功"},
                }),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 2},
                }),
                ("message_stop", {"type": "message_stop"}),
            ]

            self._send_events(events)

        def _send_events(self, events: list[tuple[str, dict[str, Any]]]) -> None:
            encoded = "".join(
                f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                for name, data in events
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


@pytest.mark.asyncio
async def test_anthropic_text_tool_and_stream_reach_real_messages_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(requests))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LOOPBACK_ANTHROPIC_KEY", "loopback-anthropic-secret")
    catalog = parse_provider_catalog_json(json.dumps({
        "provider": {
            "loopback": {
                "npm": "@ai-sdk/anthropic",
                "options": {
                    "baseURL": f"http://127.0.0.1:{server.server_address[1]}",
                    "apiKey": "{env:LOOPBACK_ANTHROPIC_KEY}",
                },
                "models": {"claude": {"upstreamId": "vendor/model-v2"}},
            }
        }
    }))
    router = ModelRouter(
        ModelConfig(provider="loopback", default_model="claude", max_tokens=128),
        catalog=catalog,
    )
    tools = [{
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "读取文件",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }]

    try:
        text = await router.call([{"role": "user", "content": "你好"}])
        tool = await router.call(
            [{"role": "user", "content": "读取 README"}],
            tools=tools,
        )
        chunks = [
            chunk
            async for chunk in router.stream(
                [{"role": "user", "content": "流式回答"}]
            )
        ]
        tool_chunks = [
            chunk
            async for chunk in router.stream(
                [{"role": "user", "content": "流式读取 README"}],
                tools=tools,
            )
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert text.content == "loopback-ok"
    assert text.usage.total_tokens == 8
    assert tool.tool_calls[0]["id"] == "toolu_1"
    assert tool.tool_calls[0]["function"]["name"] == "file_read"
    assert json.loads(tool.tool_calls[0]["function"]["arguments"]) == {
        "path": "README.md"
    }
    assert "".join(chunk.token for chunk in chunks) == "流式成功"
    assert any(chunk.usage and chunk.usage.total_tokens == 6 for chunk in chunks)
    final_tool_call = next(chunk.tool_call for chunk in tool_chunks if chunk.tool_call)
    assert final_tool_call[0]["id"] == "toolu_stream"
    assert final_tool_call[0]["function"]["name"] == "file_read"
    assert json.loads(final_tool_call[0]["function"]["arguments"]) == {
        "path": "README.md"
    }
    assert any(
        chunk.usage and chunk.usage.total_tokens == 11 for chunk in tool_chunks
    )
    assert [request[0] for request in requests] == ["/v1/messages"] * 4
    assert all(
        request[1].get("x-api-key") == "loopback-anthropic-secret"
        for request in requests
    )
    assert requests[0][2]["messages"] == [{
        "role": "user",
        "content": [{"type": "text", "text": "你好"}],
    }]
    assert requests[1][2]["tools"][0]["input_schema"]["required"] == ["path"]
    assert "stream_options" not in requests[2][2]
    assert "stream_options" not in requests[3][2]
