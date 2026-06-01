"""Smoke tests for all analysis tools without a model router."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from naumi_agent.tools.analysis import create_analysis_tools


def _write_sample_source(path: Path) -> None:
    path.write_text(
        """
import logging

logger = logging.getLogger(__name__)

def add(a: int, b: int) -> int:
    assert a >= 0
    return a + b

class Worker:
    def run(self) -> int:
        return add(1, 2)
""".strip(),
        encoding="utf-8",
    )


def _smoke_args(path: Path) -> dict[str, dict[str, object]]:
    return {
        "analysis_chaos": {"target": str(path)},
        "analysis_scale": {"qps": 100, "target": str(path)},
        "analysis_state": {"target": str(path)},
        "analysis_vibe": {"description": "构建一个计数器 demo"},
        "analysis_eval": {"target": str(path)},
        "analysis_page": {
            "context_window": 1000,
            "session_context": "目标：smoke。pytest passed。",
        },
        "analysis_heal": {"error_log": "Traceback: AssertionError", "target": str(path)},
        "analysis_dspy": {"description": "优化分类 prompt", "target": str(path)},
        "analysis_graph": {"target": str(path)},
        "analysis_mcts": {"problem": "如何修复测试失败"},
        "analysis_route": {"task": "修复 Python 单元测试"},
        "analysis_speculate": {"target": str(path)},
        "analysis_jit": {"task": "统计 Python 文件函数数量"},
        "analysis_pointer": {"target": str(path)},
        "analysis_cooe": {"task": "拆分分析任务", "target": str(path)},
        "analysis_sleep": {"session_context": "已完成 smoke。下一步复查。", "target": str(path)},
        "analysis_entropy": {"context": "目标很清晰。目标很清晰。", "goal": "完成 smoke"},
        "analysis_ooda": {"target": str(path)},
        "analysis_probe": {"task": "验证 add 行为"},
        "analysis_hook": {"task": "分析授权测试程序", "target_type": "Python"},
        "analysis_vision": {"task": "从表格截图提取数据"},
        "analysis_spar": {"task": str(path)},
        "analysis_world": {"target": str(path)},
        "analysis_fusion": {"target": str(path)},
        "analysis_consensus": {"target": str(path)},
        "analysis_pid": {"target": str(path)},
        "analysis_zkp": {"target": str(path)},
        "analysis_genesis": {"target": str(path)},
        "analysis_macro": {"task": str(path)},
        "analysis_cosmos": {"target": str(path)},
        "analysis_watchdog": {"target": str(path)},
        "analysis_supervisor": {"target": str(path)},
        "analysis_autopsy": {"target": str(path)},
        "self_review": {},
    }


@pytest.mark.asyncio
async def test_all_analysis_tools_return_deterministic_output_without_router(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "sample.py"
    _write_sample_source(sample)
    args_by_name = _smoke_args(sample)

    with (
        patch("naumi_agent.tools.analysis._global_router", None),
        patch(
            "naumi_agent.tools.analysis._find_agent_source_dir",
            return_value=str(tmp_path),
        ),
    ):
        failures: list[str] = []
        tools = create_analysis_tools()
        for tool in tools:
            args = args_by_name.get(tool.name)
            if args is None:
                failures.append(f"{tool.name}: missing smoke args")
                continue
            try:
                result = await asyncio.wait_for(tool.execute(**args), timeout=10)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{tool.name}: {type(exc).__name__}: {exc}")
                continue
            if not isinstance(result, str) or not result.strip():
                failures.append(f"{tool.name}: empty result")
            if "Router 未注入" in result:
                failures.append(f"{tool.name}: old router unavailable text")

    assert len(tools) == 34
    assert not failures
