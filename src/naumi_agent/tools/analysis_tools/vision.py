"""Screen-based data extraction analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.vision import (
    build_vision_report,
    scan_vision,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]

VISION_SYSTEM = """\
You are an AI Vision Data Extraction architect designing screen-based
data pipelines for authorized pages, user-provided screenshots, and
visible data samples.

## Boundaries
- Do not bypass login, captcha, access control, rate limits, paywalls, or
  terms-of-service restrictions.
- Treat screenshots as evidence, not as permission. The user must provide
  an authorized session or captured samples.
- Never invent screen values from a description. Require screenshot
  inventory, image hashes, ROI contracts, and validation rules.
- If the task mentions captcha, WAF, login wall, token, or access control,
  keep the plan at authorized capture and compliance review.

## Core Principle
When structured APIs are unavailable or unreliable, use screenshots as a
verifiable fallback. First fix sample identity, viewport, timestamp, image
dimensions, and ROI definitions; then choose OCR, table segmentation, chart
parsing, or multimodal extraction.

## Output Format

### 1. Access and Evidence Assessment
- Authorized data source and capture assumptions
- Screenshots or browser state required before extraction
- Risks that block automation

### 2. Vision Pipeline Design
- Capture strategy with viewport, timing, and sample identity
- ROI detection strategy for tables, charts, text, and numbers
- Extractor choice and dependencies
- Validation rules for every field
- Output schema and metadata

### 3. Accuracy Strategy
- Confidence thresholds
- Cross-checks against units, ranges, totals, timestamps, or prior frames
- Regression samples and failure reporting

### 4. Fallback Mechanisms
- Prefer API/HTML parsing when lawful and stable
- Use manual ROI review when layout changes
- Mark unknown fields explicitly instead of guessing

### 5. Cost and Speed Trade-off
- Expected latency and compute cost
- When visual extraction is justified versus overkill

Provide concrete, implementable steps only inside these boundaries.
"""


class VisionTool(Tool):
    """Authorized screen-based data extraction analysis tool."""

    def __init__(
        self,
        *,
        router_getter: RouterGetter | None = None,
        run_analysis: RunAnalysis | None = None,
    ) -> None:
        self._router_getter = router_getter or (lambda: None)
        self._run_analysis = run_analysis

    @property
    def name(self) -> str:
        return "analysis_vision"

    @property
    def description(self) -> str:
        return (
            "AI 视觉数据提取：当传统 API/HTTP 不稳定或不可用时，"
            "基于授权页面或用户提供截图设计视觉管线——"
            "截屏→检测→OCR→结构化，并通过 ROI 契约验证结果。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要提取的数据来源和目标描述",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        *,
        task: str,
        **kwargs: Any,
    ) -> str:
        scan_evidence = scan_vision(task)
        deterministic = build_vision_report(task, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性视觉提取方案。"

        user_msg = (
            f"## 数据提取需求\n{task}\n\n"
            f"## 视觉方案扫描\n{scan_evidence}\n"
            f"\n## 确定性视觉方案\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, VISION_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Vision 增强\n" + enhanced
