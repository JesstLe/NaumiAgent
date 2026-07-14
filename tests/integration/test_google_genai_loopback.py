"""Real local HTTP verification for the native Google GenAI adapter."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest

from naumi_agent.config.settings import ModelConfig
from naumi_agent.model.catalog import parse_provider_catalog_json
from naumi_agent.model.discovery import ModelDiscoveryService
from naumi_agent.model.router import ModelRouter

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "读取工作区文件",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }
]


def _text_response(text: str, *, prompt: int, output: int) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": output,
            "totalTokenCount": prompt + output,
        },
        "modelVersion": "gemini-loopback",
    }


def _tool_response() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": "file_read",
                                "args": {"path": "README.md"},
                            }
                        }
                    ],
                },
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 7,
            "candidatesTokenCount": 4,
            "totalTokenCount": 11,
        },
        "modelVersion": "gemini-loopback",
    }


def _parts(body: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for content in body.get("contents", []):
        if not isinstance(content, dict):
            continue
        for part in content.get("parts", []):
            if isinstance(part, dict):
                parts.append(part)
    return parts


@pytest.mark.asyncio
async def test_google_genai_real_loopback_text_tools_stream_and_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self, body: dict[str, Any] | None) -> None:
            requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {
                        name.casefold(): value for name, value in self.headers.items()
                    },
                    "body": body,
                }
            )

        def _send_json(self, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            self._record(None)
            if urlsplit(self.path).path != "/v1beta/models":
                self.send_error(404)
                return
            self._send_json(
                {
                    "models": [
                        {
                            "name": "models/gemini-loopback",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/embedding-loopback",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ]
                }
            )

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            self._record(body)
            path = urlsplit(self.path).path
            if path == (
                "/v1beta/models/gemini-loopback:streamGenerateContent"
            ):
                events = (
                    [_tool_response()]
                    if body.get("tools")
                    else [
                        _text_response("流式", prompt=3, output=1),
                        _text_response("成功", prompt=3, output=2),
                    ]
                )
                encoded = "".join(
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    for event in events
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return
            if path != "/v1beta/models/gemini-loopback:generateContent":
                self.send_error(404)
                return

            parts = _parts(body)
            if any(
                "functionResponse" in part or "function_response" in part
                for part in parts
            ):
                self._send_json(_text_response("tool-result-ok", prompt=9, output=2))
            elif body.get("tools"):
                self._send_json(_tool_response())
            else:
                self._send_json(_text_response("loopback-ok", prompt=5, output=3))

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1beta"
    monkeypatch.setenv("GEMINI_LOOPBACK_KEY", "loopback-google-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "ambient-must-not-win")
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-must-not-win")

    catalog = parse_provider_catalog_json(
        json.dumps(
            {
                "providers": {
                    "google": {
                        "apiFormat": "google_genai",
                        "baseURL": base_url,
                        "auth": {
                            "type": "api_key_header",
                            "env": "GEMINI_LOOPBACK_KEY",
                            "header": "X-Goog-Api-Key",
                        },
                        "models": {},
                        "discovery": {
                            "enabled": True,
                            "path": "/models",
                            "ttlSeconds": 60,
                        },
                    }
                }
            }
        ),
        source="/tmp/google-loopback-providers.json",
    )
    discovery = ModelDiscoveryService(catalog)
    router = ModelRouter(
        ModelConfig(
            provider="google",
            default_model="gemini-loopback",
            fast_model="gemini-loopback",
            reasoning_model="gemini-loopback",
            max_tokens=64,
        ),
        catalog=catalog,
        discovery_service=discovery,
    )

    try:
        listings = await router.list_available_models("google", refresh=True)
        identity = router.get_runtime_identity("gemini-loopback")
        text = await router.call(
            [
                {"role": "system", "content": "只输出结果"},
                {"role": "user", "content": "你好"},
            ]
        )
        tool = await router.call(
            [{"role": "user", "content": "读取 README"}],
            tools=TOOLS,
        )
        tool_call = tool.tool_calls[0]
        final = await router.call(
            [
                {"role": "user", "content": "读取 README"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call],
                },
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": "file_read",
                    "content": "# NaumiAgent",
                },
            ],
            tools=TOOLS,
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
                tools=TOOLS,
            )
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert [model.id for model in listings[0].models] == ["gemini-loopback"]
    assert identity.provider == "google"
    assert identity.api_format == "google_genai"
    assert identity.source == "catalog"
    assert text.content == "loopback-ok"
    assert text.finish_reason == "stop"
    assert text.usage.total_tokens == 8
    assert tool_call["function"]["name"] == "file_read"
    assert json.loads(tool_call["function"]["arguments"]) == {
        "path": "README.md"
    }
    assert final.content == "tool-result-ok", json.dumps(
        requests[3]["body"],
        ensure_ascii=False,
        indent=2,
    )
    assert final.finish_reason == "stop"
    assert "".join(chunk.token for chunk in chunks) == "流式成功"
    assert any(chunk.usage and chunk.usage.total_tokens > 0 for chunk in chunks)
    assert chunks[-1].finish_reason == "stop"
    streamed_tool_call = next(
        chunk.tool_call for chunk in tool_chunks if chunk.tool_call
    )
    assert streamed_tool_call[0]["function"]["name"] == "file_read"
    assert json.loads(streamed_tool_call[0]["function"]["arguments"]) == {
        "path": "README.md"
    }
    assert any(
        chunk.usage and chunk.usage.total_tokens == 11
        for chunk in tool_chunks
    )

    inference_requests = [request for request in requests if request["method"] == "POST"]
    assert len(inference_requests) == 5
    assert [request["path"] for request in inference_requests] == [
        "/v1beta/models/gemini-loopback:generateContent",
        "/v1beta/models/gemini-loopback:generateContent",
        "/v1beta/models/gemini-loopback:generateContent",
        "/v1beta/models/gemini-loopback:streamGenerateContent?alt=sse",
        "/v1beta/models/gemini-loopback:streamGenerateContent?alt=sse",
    ]
    text_body = inference_requests[0]["body"]
    system_instruction = text_body.get("system_instruction") or text_body.get(
        "systemInstruction"
    )
    assert system_instruction is not None, json.dumps(
        text_body,
        ensure_ascii=False,
        indent=2,
    )
    assert system_instruction["parts"] == [
        {"text": "只输出结果"}
    ]
    assert text_body["contents"] == [
        {"role": "user", "parts": [{"text": "你好"}]}
    ]
    assert all(
        request["headers"].get("x-goog-api-key") == "loopback-google-secret"
        for request in requests
    )
    assert not any(
        "ambient-must-not-win" in json.dumps(request["body"], ensure_ascii=False)
        for request in inference_requests
    )
    assert not any(
        "GEMINI_LOOPBACK_KEY" in json.dumps(request["body"], ensure_ascii=False)
        or "supportedGenerationMethods"
        in json.dumps(request["body"], ensure_ascii=False)
        for request in inference_requests
    )
    assert any(
        "function_declarations" in json.dumps(request["body"], ensure_ascii=False)
        for request in inference_requests
    )
    assert any(
        "function_call" in part
        for request in inference_requests
        for part in _parts(request["body"])
    )
    assert any(
        "function_response" in part
        for request in inference_requests
        for part in _parts(request["body"])
    )
    assert all(
        "stream_options" not in (request["body"] or {})
        for request in inference_requests
    )
