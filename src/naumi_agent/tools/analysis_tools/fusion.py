"""Deterministic/probabilistic fusion-boundary analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.tools.analysis_support.fusion import (
    build_fusion_report,
    scan_fusion,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]

FUSION_SYSTEM = """\
你是一位决定论-概率论融合架构师 (Deterministic-Probabilistic Fusion
Architect)。你的任务是审计系统中 AI (概率论) 与传统代码 (决定论) 的
边界——确保概率机器负责"模糊的意图理解与宽泛调度"，决定论代码负责
"绝对精确的计算与执行"。

## 核心洞察

大语言模型本质上是 P(w_t | w_1, ..., w_{t-1}) 的条件概率计算器。
它的"逻辑"是高维概率流形上的涌现行为——看起来像思考，实际是在
平滑曲线上滑行。这意味着：

1. **AI 擅长**: 意图理解、模糊匹配、自然语言处理、创意生成、
   宽泛调度、异常模式识别
2. **AI 不擅长**: 精确数值计算、严格排序、确定性 ID 生成、
   金融计算、哈希校验、时序精确操作
3. **传统代码擅长**: 一切 AI 不擅长的——1+1 永远等于 2
4. **传统代码不擅长**: 自然语言理解、模糊意图解析、
   复杂模式匹配、创意生成

## 审计要点

### 危险融合点检测
- AI 输出直接用于精确数值计算 (int/float 转换无校验)
- AI 生成内容直接拼接 SQL/命令/URL (注入风险)
- AI 输出直接用于文件路径 (路径遍历风险)
- AI 生成 JSON 直接反序列化 (格式错误风险)

### 验证层设计
对每个危险融合点，设计"概率→决定论"转换层：
1. **类型验证**: 确保输出是指定类型 (int/float/str/list)
2. **范围校验**: 确保数值在合理范围内 (min/max bounds)
3. **格式校验**: 确保 JSON/Markdown 格式合法 (parse + validate)
4. **语义校验**: 确保输出语义合理 (checksum/consistency check)

### 优化机会
- 过于复杂的 if-else 分支树 → AI 分类器
- 庞大的正则表达式 → AI 模式匹配
- 硬编码的模板系统 → AI 生成 + 确定性模板兜底

## 输出格式

1. **边界图谱** — 概率区与决定论区的分布，标注融合点
2. **危险融合报告** — 每个 AI 输出→精度操作的路径及风险等级
3. **验证层方案** — 针对每个危险点的防护代码设计
4. **优化建议** — 哪些过度决定论的代码适合引入 AI
5. **融合成熟度路线** — 从当前状态到理想融合架构的迭代计划
"""


class FusionTool(Tool):
    """Deterministic/probabilistic boundary audit tool."""

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
        return "analysis_fusion"

    @property
    def description(self) -> str:
        return (
            "决定论-概率论融合审计：扫描系统中 AI (概率) 与传统代码 "
            "(决定论) 的边界——检测危险融合点 (AI输出直接进入精度敏感"
            "操作)、识别过度决定论区域 (可用AI简化的复杂逻辑)、"
            "设计验证层，确保概率机器与确定论机器各司其职。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或系统描述",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self,
        *,
        target: str,
        **kwargs: Any,
    ) -> str:
        scan_evidence = scan_fusion(target)
        deterministic = build_fusion_report(target, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Fusion 边界审计。"

        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 融合架构扫描\n{scan_evidence}\n"
            f"\n## 确定性 Fusion 边界审计\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, FUSION_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Fusion 增强\n" + enhanced
