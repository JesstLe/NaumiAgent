"""Live integration: pi actually calling the naumi analysis extension.

Skips unless the pi binary, the Node selftest, and a usable Zhipu gateway
key are all present. Verifies the full chain: pi loads the extension, the
model decides to call naumi_chaos, the Python scanner runs, and tool
events carry the tool name.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from naumi_agent.pi_engine.rpc import PiRpcClient

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION = REPO_ROOT / "pi_extensions" / "naumi-analysis.js"


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    target = tmp_path / "service"
    target.mkdir()
    (target / "app.py").write_text(
        "import requests\n\n"
        "DB_HOST = '10.0.0.5'\n\n"
        "def get(uid):\n"
        "    try:\n"
        "        return requests.get(f'http://users.internal/{uid}').json()\n"
        "    except:\n"
        "        return None\n",
        encoding="utf-8",
    )
    return target


def _zai_key() -> str:
    key = os.environ.get("ZAI_CODING_CN_API_KEY")
    if not key and "bigmodel" in os.environ.get("OPENAI_BASE_URL", ""):
        key = os.environ.get("OPENAI_API_KEY")
    return key or ""


def _prerequisites_ok() -> bool:
    if shutil.which("pi") is None:
        return False
    if not _zai_key():
        return False
    return EXTENSION.is_file()


async def test_pi_calls_naumi_chaos_tool(fixture_dir: Path) -> None:
    if not _prerequisites_ok():
        pytest.skip("需要 pi 二进制、智谱网关 Key 与仓库内扩展文件")
    tool_events: list[dict] = []
    settled = asyncio.Event()

    def on_event(record: dict) -> None:
        if record.get("type") in {
            "tool_execution_start",
            "tool_execution_end",
        }:
            tool_events.append(record)
        if record.get("type") == "agent_settled":
            settled.set()

    client = await PiRpcClient.start(
        binary="pi",
        provider="zai-coding-cn",
        model="glm-4.7",
        cwd=str(fixture_dir.parent),
        extra_args=["-e", str(EXTENSION), "--no-session"],
        event_handler=on_event,
        env={
            "ZAI_CODING_CN_API_KEY": _zai_key(),
            "NAUMI_PYTHON": sys.executable,
        },
    )
    try:
        await client.request("new_session", timeout=30)
        await client.prompt(
            "请调用 naumi_chaos 工具扫描 service 目录，"
            "然后用两句话总结扫描发现的问题。"
        )
        await asyncio.wait_for(settled.wait(), timeout=240)
    finally:
        await client.stop()

    chaos_calls = [e for e in tool_events if e.get("toolName") == "naumi_chaos"]
    assert chaos_calls, (
        "模型未调用 naumi_chaos；收到的事件: "
        + json.dumps([e.get("toolName") for e in tool_events], ensure_ascii=False)
    )
    end_events = [
        e
        for e in chaos_calls
        if e.get("type") == "tool_execution_end" and not e.get("isError")
    ]
    assert end_events, "naumi_chaos 被调用但未成功返回"
    result_text = "".join(
        str(block.get("text") or "")
        for block in (end_events[0].get("result") or {}).get("content", [])
        if isinstance(block, dict)
    )
    payload = json.loads(result_text)
    assert payload["ok"] is True
    assert payload["files_scanned"] >= 1
    assert "裸 except" in payload["report"]
