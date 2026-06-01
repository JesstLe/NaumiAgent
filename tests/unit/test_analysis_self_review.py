"""Self-review analysis tool tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.tools.analysis import (
    SelfReviewTool,
    _build_self_review_inventory_script,
    _build_self_review_report,
    _resolve_target,
    _scan_self_review,
)
from naumi_agent.tools.analysis_tools.self_review import (
    SelfReviewTool as SplitSelfReviewTool,
)


def _write_self_review_source(path: Path) -> None:
    path.write_text(
        """
import logging

logger = logging.getLogger(__name__)
token = "not-a-real-secret"

class ExampleTool(Tool):
    pass

def public_function(value):
    try:
        logger.info("debug trace")
        return value
    except:
        print("bad")
""".strip(),
        encoding="utf-8",
    )


def test_scan_self_review_reports_static_health(tmp_path: Path) -> None:
    source = tmp_path / "self_review_case.py"
    _write_self_review_source(source)
    files = _resolve_target(str(tmp_path))
    source_text = "\n".join(path.read_text(encoding="utf-8") for path in files)

    scan = _scan_self_review(files, source_text)

    assert "源文件" in scan
    assert "裸 except" in scan
    assert "疑似硬编码密钥" in scan
    assert "logger 调用" in scan


def test_build_self_review_inventory_script_is_runnable(tmp_path: Path) -> None:
    source = tmp_path / "self_review_case.py"
    _write_self_review_source(source)
    script = tmp_path / "self_review_inventory.py"
    script.write_text(
        _build_self_review_inventory_script(str(tmp_path)),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["summary"]["bare_except"] >= 1
    assert payload["summary"]["hardcoded_secret"] >= 1
    assert payload["summary"]["tool_classes"] >= 1
    assert payload["self_review_contract"]["minimum_self_evolution_loop"]


def test_build_self_review_report_contains_contract(tmp_path: Path) -> None:
    source = tmp_path / "self_review_case.py"
    _write_self_review_source(source)
    files = _resolve_target(str(tmp_path))
    source_text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    report = _build_self_review_report(
        str(tmp_path),
        "safety",
        _scan_self_review(files, source_text),
    )

    assert "## Self-Review 确定性自审查报告" in report
    assert "## Self-Review Inventory Script" in report
    assert "## Self-Evolution Contract" in report
    assert "self_review_contract" in report


class TestSelfReviewTool:
    @pytest.mark.asyncio
    async def test_execute_without_router_returns_deterministic_report(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "self_review_case.py"
        _write_self_review_source(source)

        with (
            patch("naumi_agent.tools.analysis._global_router", None),
            patch(
                "naumi_agent.tools.analysis._find_agent_source_dir",
                return_value=str(tmp_path),
            ),
        ):
            output = await SelfReviewTool().execute(focus="safety")

        assert "## Self-Review 确定性自审查报告" in output
        assert "Self-Review Inventory Script" in output
        assert "模型路由未初始化" in output
        assert "Router 未注入" not in output

    @pytest.mark.asyncio
    async def test_execute_with_router_keeps_report_and_adds_enhancement(
        self, tmp_path: Path,
    ) -> None:
        source = tmp_path / "self_review_case.py"
        _write_self_review_source(source)
        mock_response = ModelResponse(
            content="增强：优先修复 bare except。",
            usage=TokenUsage(input_tokens=16, output_tokens=7, total_tokens=23),
            model="test",
        )

        with (
            patch("naumi_agent.tools.analysis._global_router") as router,
            patch(
                "naumi_agent.tools.analysis._find_agent_source_dir",
                return_value=str(tmp_path),
            ),
        ):
            router.call = AsyncMock(return_value=mock_response)
            output = await SelfReviewTool().execute(focus="quality")

        assert "## Self-Review 确定性自审查报告" in output
        assert "## LLM Self-Review 增强" in output
        assert "bare except" in output

    @pytest.mark.asyncio
    async def test_split_tool_uses_injected_runner_and_source_dir(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "self_review_case.py"
        _write_self_review_source(source)
        calls: list[tuple[object, str, str]] = []

        async def run_analysis(router: object, system: str, user_msg: str) -> str:
            calls.append((router, system, user_msg))
            return "增强：优先修复 bare except。"

        router = object()
        output = await SplitSelfReviewTool(
            router_getter=lambda: router,
            run_analysis=run_analysis,
            source_dir_getter=lambda: str(tmp_path),
        ).execute(focus="quality")

        assert "## Self-Review 确定性自审查报告" in output
        assert "## LLM Self-Review 增强" in output
        assert "bare except" in output
        assert calls
        assert calls[0][0] is router
        assert "自审查分析引擎" in calls[0][1]
        assert "请重点关注: quality" in calls[0][2]
