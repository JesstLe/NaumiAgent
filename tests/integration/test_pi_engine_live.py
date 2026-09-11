"""Integration tests against a real locally-installed pi binary.

These are skipped automatically when ``pi`` (or the optional model key) is
not available, so CI without the external engine stays green.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys

import pytest

from naumi_agent.pi_engine.rpc import PiRpcClient

pytestmark = pytest.mark.integration

_PI_BIN = shutil.which("pi")


@pytest.fixture
async def real_pi():
    if _PI_BIN is None:
        pytest.skip("本机未安装 pi（npm install -g @earendil-works/pi-coding-agent）")
    client = await PiRpcClient.start(binary=_PI_BIN, cwd="/tmp")
    yield client
    await client.stop()


async def test_real_pi_state_and_bash(real_pi: PiRpcClient) -> None:
    state = await real_pi.get_state()
    assert state.get("sessionId")
    assert isinstance(state.get("model"), dict)

    result = await real_pi.request(
        "bash", command="echo naumi-live-ok", timeout=30
    )
    assert result.get("exitCode") == 0
    assert "naumi-live-ok" in str(result.get("output", ""))


async def test_real_pi_conversation_streams_to_settled() -> None:
    zai_key = os.environ.get("ZAI_CODING_CN_API_KEY")
    # The OpenAI key in this environment points at the Zhipu (bigmodel)
    # gateway, so it can be reused for pi's zai-coding-cn provider.
    if not zai_key and "bigmodel" in os.environ.get("OPENAI_BASE_URL", ""):
        zai_key = os.environ.get("OPENAI_API_KEY")
    if not zai_key:
        pytest.skip("未检测到 ZAI_CODING_CN_API_KEY（或可复用的智谱网关 Key）")

    settled = asyncio.Event()
    text_parts: list[str] = []

    def on_event(record: dict) -> None:
        if record.get("type") == "agent_settled":
            settled.set()
        update = record.get("assistantMessageEvent") or {}
        if (
            record.get("type") == "message_update"
            and update.get("type") == "text_delta"
        ):
            text_parts.append(str(update.get("delta") or ""))

    client = await PiRpcClient.start(
        binary=_PI_BIN or "pi",
        provider="zai-coding-cn",
        model="glm-4.7",
        cwd="/tmp",
        event_handler=on_event,
        env={"ZAI_CODING_CN_API_KEY": zai_key},
    )
    try:
        await client.request("new_session", timeout=30)
        await client.prompt("只回复两个字符:ok")
        await asyncio.wait_for(settled.wait(), timeout=180)
        assert "".join(text_parts).strip() != ""
    finally:
        await client.stop()


async def test_pi_bridge_module_speaks_terminal_protocol(tmp_path) -> None:
    """End-to-end without a model key: spawn the bridge module, negotiate
    hello, and drive the /engine slash command over real JSONL."""
    if _PI_BIN is None:
        pytest.skip("本机未安装 pi")
    project_root = tmp_path / "ws"
    project_root.mkdir()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "naumi_agent.pi_engine",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(project_root),
        limit=1024 * 1024,
    )
    assert proc.stdin is not None and proc.stdout is not None

    async def read_until(predicate, timeout: float = 60.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=deadline - loop.time())
            if not raw:
                break
            text = raw.decode(errors="replace").strip()
            if not text:
                continue
            record = json.loads(text)
            if predicate(record):
                return record
        raise AssertionError("bridge 未返回预期事件")

    def send(record: dict) -> None:
        proc.stdin.write(
            (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        )

    try:
        send(
            {
                "type": "hello",
                "payload": {
                    "client": "integration-test",
                    "minimum_version": 1,
                    "maximum_version": 1,
                    "capabilities": ["typed_ui_messages"],
                },
            }
        )
        ready = await read_until(lambda r: r["type"] == "ready")
        assert ready["payload"]["engine"] == "pi"

        send({"type": "submit", "payload": {"text": "/engine"}, "request_id": "r1"})
        notice = await read_until(
            lambda r: r["type"] == "ui/message"
            and r["payload"].get("type") == "system_notice"
            and "engine=pi" in r["payload"].get("content", "")
        )
        assert "naumi --engine naumi" in notice["payload"]["content"]
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            proc.kill()
