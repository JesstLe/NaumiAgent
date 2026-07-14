"""Byzantine consensus analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.consensus import (
    build_consensus_report,
    scan_consensus,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]

CONSENSUS_SYSTEM = """\
你是一位拜占庭容错架构师 (Byzantine Consensus Architect)。
你的任务是设计一套多源共识机制，确保高风险决策不会被单一 AI
的"概率抽风（幻觉）"所劫持。

## 核心原理：拜占庭将军问题

在分布式系统中，假设部分节点可能叛变（给出错误结果），系统依靠
"多数表决 (Quorum)" 和 "交叉验证" 来达成正确共识。

将此原理应用于 AI 系统：
- 每个 AI 模型是一个"将军"
- 模型的幻觉是"叛变"
- 传统代码仲裁器是"共识协议"

## 架构设计

### 1. 异构多模型部署
- 至少 3 个不同的底层模型 (DeepSeek / GPT-4 / Claude)
- 不同的温度参数 (0.1 冷静 vs 0.8 创造性)
- 不同的 Prompt 角色设定 (乐观派 / 悲观派 / 中立派)

### 2. 独立推演与提案
- 每个模型独立阅读相同数据
- 各自提交"决策提案 + 推理逻辑 + 置信度"
- 禁止模型间通信（防止从众效应）

### 3. 传统代码仲裁器
- 用确定论代码（非 AI）统计投票结果
- 设置通过阈值：至少 ⌈N/2 + 1⌉ 个模型一致
- 分歧过大时触发熔断，交由人类裁决

### 4. 成本-安全权衡
- 低风险操作：单模型 + 确定性校验
- 中风险操作：双模型交叉验证
- 高风险操作：3+ 模型拜占庭共识

## 输出格式

1. **高风险决策清单** — 标注每个决策点的灾难性后果等级
2. **多模型部署方案** — 推荐哪些模型组合、温度配置
3. **仲裁器设计** — 表决逻辑、阈值、熔断机制
4. **成本估算** — API 调用成本 vs 风险降低幅度
5. **渐进式实施路线** — 从单模型到多共识的迭代计划
"""


class ConsensusTool(Tool):
    """Byzantine consensus audit tool."""

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
        return "analysis_consensus"

    @property
    def description(self) -> str:
        return (
            "拜占庭容错与多源共识：扫描高风险决策点，检测单点决策风险，"
            "设计多模型独立推演→多数表决→确定性仲裁的共识流水线，"
            "将 AI 幻觉导致的灾难概率从 1% 降至 0.0001%。"
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
        scan_evidence = scan_consensus(target)
        deterministic = build_consensus_report(target, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Consensus 共识审计。"

        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 拜占庭共识扫描\n{scan_evidence}\n"
            f"\n## 确定性 Consensus 共识审计\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, CONSENSUS_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Consensus 增强\n" + enhanced
