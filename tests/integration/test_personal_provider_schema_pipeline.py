"""Actual model HTTP/export/tool/SQLite path without any external provider call."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import keyring
import pytest

from naumi_agent.config.settings import AppConfig, ModelConfig
from naumi_agent.runtime.composition import create_agent_engine

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


def _untyped_choices(value: Any, path: str = "$") -> list[str]:
    missing = []
    if isinstance(value, dict):
        if ("enum" in value or "const" in value) and "type" not in value:
            missing.append(path)
        for name, child in value.items():
            missing.extend(_untyped_choices(child, f"{path}.{name}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            missing.extend(_untyped_choices(child, f"{path}[{index}]"))
    return missing


class _SchemaProtocolServer:
    """Enforce the observed provider requirement before producing any tool call."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.rejections: list[list[str]] = []
        self.accepted_tools: list[set[str]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers["content-length"]))
                request = json.loads(raw)
                owner.requests.append(request)
                missing = [
                    path
                    for tool in request.get("tools", [])
                    for path in _untyped_choices(tool["function"]["parameters"])
                ]
                if missing:
                    owner.rejections.append(missing)
                    self.answer(400, {"error": {
                        "message": f"schema node requires explicit type: {missing[0]}",
                        "type": "invalid_request_error",
                    }})
                    return
                owner.accepted_tools.append({
                    tool["function"]["name"] for tool in request["tools"]
                })
                messages = request["messages"]
                outputs = [message for message in messages if message["role"] == "tool"]
                if outputs:
                    message = {"role": "assistant", "content": outputs[-1]["content"]}
                    finish_reason = "stop"
                else:
                    tool_name = next(
                        message["content"].split()[-1]
                        for message in messages if message["role"] == "user"
                    )
                    operation = {
                        "personal_items": "list",
                        "personal_connections": "context",
                        "personal_actions": "list_actions",
                    }[tool_name]
                    message = {
                        "role": "assistant", "content": "", "tool_calls": [{
                            "id": f"call-{tool_name}", "type": "function",
                            "function": {"name": tool_name,
                                         "arguments": json.dumps({"operation": operation})},
                        }],
                    }
                    finish_reason = "tool_calls"
                self.answer(200, {
                    "id": f"chatcmpl-schema-{len(owner.requests)}",
                    "object": "chat.completion", "created": 1, "model": request["model"],
                    "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                })

            def answer(self, status: int, value: dict[str, Any]) -> None:
                encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> _SchemaProtocolServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


def _logical_databases(root: Path) -> dict[str, Any]:
    inventory = {}
    for path in root.rglob("*.db"):
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            schema = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            tables = {}
            for table, in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                escaped = table.replace('"', '""')
                rows = connection.execute(f'SELECT * FROM "{escaped}"').fetchall()
                tables[table] = sorted(repr(row) for row in rows)
            inventory[path.relative_to(root).as_posix()] = {
                "schema": schema, "tables": tables,
                "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            }
    return inventory


@pytest.mark.asyncio
async def test_typed_personal_schemas_reach_real_read_tools_and_original_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    credential_calls: list[str] = []

    def forbidden_credential_backend(*_args: Any, **_kwargs: Any) -> None:
        credential_calls.append("unexpected credential backend access")
        raise AssertionError("Local schema acceptance must not consult native credentials")

    for operation in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, operation, forbidden_credential_backend)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with _SchemaProtocolServer() as provider:
        engine = create_agent_engine(AppConfig(
            workspace_root=str(workspace),
            personal={"data_dir": str(tmp_path / "personal")},
            memory={
                "session_db_path": str(tmp_path / "runtime" / "sessions.db"),
                "vector_db_path": str(tmp_path / "runtime" / "vectors"),
                "long_term_enabled": False,
            },
            models=ModelConfig(
                default_model="openai/gpt-4o-mini", fast_model="openai/gpt-4o-mini",
                reasoning_model="openai/gpt-4o-mini", api_base=provider.base_url,
                api_key="local-schema-protocol-only", temperature=0, max_tokens=128,
            ),
        ))
        try:
            await engine.get_or_create_session(title="本机工具合同验收")
            created = await engine.personal_items.create({
                "request_id": "schema-pipeline-original-item",
                "title": "Schema 链路原事项 7391",
                "instructions": "保留原身份，不执行、不发送、不生成新运行。",
                "assignee_id": "main", "assignee_version": 0,
                "schedule": {"mode": "once", "local_time": "2035-01-01T09:00:00",
                             "timezone": "Asia/Shanghai"},
            })
            expected_item_id = created["item_id"]
            tools = {name: engine.tool_registry.get(name) for name in (
                "personal_items", "personal_connections", "personal_actions",
            )}
            assert all(tool is not None for tool in tools.values())
            exported = [tool.to_openai_tool() for tool in tools.values() if tool is not None]
            assert engine.personal_items.store.db_path.is_file()
            # Prepare the domain's lazy, empty journals before the read-only
            # observation window; first-use schema bootstrap is not business data.
            for name, operation in (("personal_items", "list"),
                                    ("personal_connections", "context"),
                                    ("personal_actions", "list_actions")):
                await tools[name].execute(operation=operation)
            before = await asyncio.to_thread(_logical_databases, tmp_path)
            results = {}
            for tool_name, tool in tools.items():
                assert tool is not None
                messages = [{"role": "user", "content": f"SCHEMA-PIPELINE {tool_name}"}]
                first = await asyncio.wait_for(
                    engine.router.call(messages, tools=exported), timeout=30,
                )
                assert len(first.tool_calls) == 1
                call = first.tool_calls[0]
                assert call["function"]["name"] == tool_name
                arguments = tool.parse_arguments(call["function"]["arguments"])
                output = await tool.execute(**arguments)
                results[tool_name] = json.loads(output)
                messages.extend([
                    {"role": "assistant", "content": "", "tool_calls": first.tool_calls},
                    {"role": "tool", "tool_call_id": call["id"], "content": output},
                ])
                final = await asyncio.wait_for(
                    engine.router.call(messages, tools=exported), timeout=30,
                )
                assert final.content == output
                assert final.tool_calls == []
            assert [item["item_id"] for item in results["personal_items"]["items"]] == [
                expected_item_id,
            ]
            assert results["personal_items"]["items"][0]["definition"]["title"] == (
                "Schema 链路原事项 7391"
            )
            assert results["personal_connections"]["provider"] == "google_calendar"
            assert results["personal_connections"]["client_configured"] is False
            assert results["personal_actions"] == {
                "actions": [], "content_transport": "get_action",
            }
            assert await asyncio.to_thread(_logical_databases, tmp_path) == before
            assert len(provider.requests) == 6
            assert provider.rejections == []
            assert provider.accepted_tools == [set(tools)] * 6
            assert credential_calls == []
        finally:
            await engine.shutdown()
