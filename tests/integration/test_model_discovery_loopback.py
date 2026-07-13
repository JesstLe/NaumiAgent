"""Real HTTP loopback coverage for provider model discovery surfaces."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from rich.text import Text

from naumi_agent.api.routes.tools import get_config
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import ModelConfig
from naumi_agent.model.catalog import parse_provider_catalog_json
from naumi_agent.model.discovery import ModelDiscoveryService
from naumi_agent.model.router import ModelRouter


def _handler(requests: list[tuple[str, dict[str, str]]]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            requests.append((self.path, dict(self.headers)))
            if self.path == "/openai/v1/models":
                self._send_json({"data": [{"id": "gpt-loopback"}]})
                return
            if self.path == "/ollama/api/tags":
                self._send_json({"models": [{"model": "qwen-loopback:latest"}]})
                return
            self.send_error(404)

        def _send_json(self, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


@pytest.mark.asyncio
async def test_real_discovery_reaches_openai_and_ollama_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, str]]] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(requests))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LOOPBACK_OPENAI_KEY", "loopback-openai-secret")
    port = server.server_address[1]
    catalog = parse_provider_catalog_json(
        json.dumps(
            {
                "providers": {
                    "openai-loopback": {
                        "name": "OpenAI Loopback",
                        "apiFormat": "openai_chat",
                        "baseURL": f"http://127.0.0.1:{port}/openai/v1",
                        "auth": {
                            "type": "bearer",
                            "env": "LOOPBACK_OPENAI_KEY",
                        },
                        "headers": {"x-naumi-test": "openai"},
                        "models": {},
                        "discovery": {"enabled": True, "path": "/models"},
                    },
                    "ollama-loopback": {
                        "name": "Ollama Loopback",
                        "apiFormat": "ollama",
                        "baseURL": f"http://127.0.0.1:{port}/ollama",
                        "auth": {"type": "none"},
                        "models": {},
                        "discovery": {"enabled": True, "path": "/api/tags"},
                    },
                }
            }
        )
    )
    discovery = ModelDiscoveryService(catalog)
    router = ModelRouter(
        ModelConfig(
            provider="openai-loopback",
            default_model="gpt-loopback",
            fast_model="ollama-loopback/qwen-loopback:latest",
            reasoning_model="gpt-loopback",
        ),
        catalog=catalog,
        discovery_service=discovery,
    )

    try:
        listings = await asyncio.gather(
            *(router.list_available_models() for _ in range(50))
        )
        slash = Text.from_ansi(
            await execute_slash_command(
                SimpleNamespace(router=router),
                "/models",
            )
        ).plain
        engine = SimpleNamespace(
            router=router,
            config=SimpleNamespace(
                safety=SimpleNamespace(
                    permission_mode="bypass",
                    max_budget_usd=None,
                    max_turns=50,
                )
            ),
            tool_registry=SimpleNamespace(all=lambda: []),
        )
        rest = await get_config(
            SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(engine=engine))
            ),
            auth="test",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert all(len(snapshot) == 2 for snapshot in listings)
    assert router.resolve_target("gpt-loopback").canonical_model == (
        "openai-loopback/gpt-loopback"
    )
    assert router.resolve_target(
        "ollama-loopback/qwen-loopback:latest"
    ).canonical_model == (
        "ollama-loopback/qwen-loopback:latest"
    )
    assert "openai-loopback/gpt-loopback" in slash
    assert "ollama-loopback/qwen-loopback:latest" in slash
    assert {model.id for model in rest.models} == {
        "openai-loopback/gpt-loopback",
        "ollama-loopback/qwen-loopback:latest",
    }
    assert rest.max_budget_usd is None

    counts = Counter(path for path, _headers in requests)
    assert counts == Counter({"/openai/v1/models": 1, "/ollama/api/tags": 1})
    openai_headers = next(
        headers for path, headers in requests if path == "/openai/v1/models"
    )
    ollama_headers = next(
        headers for path, headers in requests if path == "/ollama/api/tags"
    )
    openai_headers = {key.lower(): value for key, value in openai_headers.items()}
    ollama_headers = {key.lower(): value for key, value in ollama_headers.items()}
    assert openai_headers["authorization"] == "Bearer loopback-openai-secret"
    assert openai_headers["x-naumi-test"] == "openai"
    assert "authorization" not in ollama_headers
