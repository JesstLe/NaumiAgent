"""Black-box probe analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.probe import (
    build_probe_report,
    scan_probe,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]

PROBE_SYSTEM = """\
You are a Black-Box Probe architect implementing anti-hallucination \
protocols for unknown/closed-source systems.

## Core Principle
**NEVER guess APIs, class names, memory addresses, or function \
signatures for systems you don't have documentation for.** Instead, \
write reconnaissance scripts that discover the real interfaces.

## The 3-Phase Protocol

### Phase 1: Probe Script Generation
Write a SAFE, HARMLESS reconnaissance script that:
- Uses reflection/introspection to enumerate available classes/methods
- Scans memory for known patterns (if applicable)
- Captures network traffic to discover API endpoints
- Dumps configuration files or log outputs
- **MUST be non-destructive** — read-only, no writes or modifications

Output a complete, runnable probe script with:
- Language selection based on the target (C# for Unity, Python for \
general, C for memory)
- Clear instructions on how to run it
- What output to expect
- What to do with the output (feed it back for Phase 2)

### Phase 2: Information Extraction Template
Provide a template for the user to paste the probe output:
- What fields to look for
- How to identify the real API names vs noise
- What to extract and bring back

### Phase 3: Development Plan (AFTER probe results)
Outline what you'll do with the real information:
- How to map discovered APIs to the user's requirements
- What the implementation will look like
- What assertions to add to catch future API changes

## Anti-Hallucination Rules
1. If you don't know the exact API, say "UNKNOWN — probe required"
2. Never fabricate function names, class names, or memory offsets
3. Always include a verification step in generated code
4. If the user provides probe results, validate them before coding
5. Mark every assumption clearly as [ASSUMPTION — verify]

## Output Format
1. Risk assessment (how much do we NOT know?)
2. Probe script (complete, runnable, non-destructive)
3. Execution instructions
4. Information extraction template
5. Development plan (conditional on probe results)
"""


class ProbeTool(Tool):
    """黑盒探测与反幻觉协议工具."""

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
        return "analysis_probe"

    @property
    def description(self) -> str:
        return (
            "黑盒探测与反幻觉协议：面对闭源/未知系统时，"
            "禁止凭空编造业务代码，先生成无害的探测脚本"
            "（反射遍历、内存扫描、网络抓包），"
            "收集真实系统信息后再进行开发。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要开发的功能描述",
                },
                "context": {
                    "type": "string",
                    "description": "已知的系统信息（SDK、文档片段等）",
                    "default": "",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        *,
        task: str,
        context: str = "",
        **kwargs: Any,
    ) -> str:
        scan_evidence = scan_probe(task, context)
        deterministic = build_probe_report(task, context, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性黑盒探测协议。"

        user_msg = f"## 开发任务\n{task}\n"
        user_msg += f"\n## 探测扫描\n{scan_evidence}\n"
        user_msg += f"\n## 确定性探测协议\n{deterministic}\n"
        if context:
            user_msg += f"\n## 已知系统信息\n{context}\n"
        enhanced = await self._run_analysis(router, PROBE_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM 探测增强\n" + enhanced
