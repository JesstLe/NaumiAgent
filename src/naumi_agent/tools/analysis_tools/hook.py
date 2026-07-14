"""Authorized hook and instrumentation analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.hook import (
    build_hook_report,
    scan_hook,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]

HOOK_SYSTEM = """\
You are a Reverse Engineering architect designing authorized, read-only
instrumentation reconnaissance for black-box system analysis.

## Boundaries
- Only discuss authorized security research, compatibility testing, and
  educational reverse engineering.
- Do not provide bypass, evasion, persistence, stealth, or anti-cheat
  defeat steps.
- If anti-debug, anti-cheat, packing, integrity checks, kernel drivers, or
  commercial protection systems appear, stop at risk identification and
  require explicit authorization plus a controlled test environment.
- UNKNOWN ABI, UNKNOWN symbol, UNKNOWN offset, and UNKNOWN API must remain
  UNKNOWN until verified by inventory output.

## Core Principle
When source code is unavailable, first collect evidence from file headers,
metadata, manifests, exports, imports, symbols, logs, and documented runtime
inspection points. Do not guess function names, offsets, or memory layouts.

## Output Format

### 1. Target Analysis
- Compilation/runtime type (native C++ / managed .NET / Java / WASM / unknown)
- Verified evidence versus assumptions
- Expected protections and compliance constraints

### 2. Reconnaissance Phase
- Read-only inventory steps and exact artifacts to collect
- Public metadata/export/import/log sources to inspect
- Verification checklist for sample identity and format

### 3. Observation Design
- Candidate observation points based only on verified evidence
- What data to capture (parameters, return values, timing, logs)
- Safe implementation options for an authorized lab environment

### 4. Stop Conditions
- Conditions that require human authorization review
- Conditions that keep fields marked UNKNOWN
- Risks that prevent dynamic instrumentation

### 5. Data Extraction Pipeline
- How captured evidence maps to the original task
- Export format and validation steps
- Regression checks to prove future runs inspect the same target

Provide concrete, implementable steps only inside these boundaries.
"""


class HookTool(Tool):
    """Authorized hook and instrumentation reconnaissance tool."""

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
        return "analysis_hook"

    @property
    def description(self) -> str:
        return (
            "底层逆向与插桩推演：根据目标程序的编译特性"
            "（原生C++/C#/Java/WASM），设计动态侦测方案，"
            "包含内存基址定位、API Hooking 和反调试风险识别。"
            "仅用于安全研究与合规逆向工程。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "逆向分析目标描述",
                },
                "target_type": {
                    "type": "string",
                    "description": "目标类型提示（可选）",
                    "default": "",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        *,
        task: str,
        target_type: str = "",
        **kwargs: Any,
    ) -> str:
        combined = f"{task} {target_type}".strip()
        scan_evidence = scan_hook(combined)
        deterministic = build_hook_report(task, target_type, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Hook 合规侦测方案。"

        user_msg = (
            f"## 逆向目标\n{task}\n\n"
            f"## 侦测扫描\n{scan_evidence}\n"
            f"\n## 确定性 Hook 方案\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, HOOK_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Hook 增强\n" + enhanced
