"""World-model audit analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.world import (
    build_world_report,
    scan_world,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]

WORLD_SYSTEM = """\
你是一位世界模型架构师 (World Model Architect)。
你的任务是将目标系统视为一个"微型世界模型"来审计——评估它对自身
领域状态的感知、因果链的理解、以及反事实推演的能力。

## 核心概念

世界模型是一个能够拟合状态转移方程 s_{t+1} = f(s_t, a_t) 的系统：
- s_t: 当前世界状态
- a_t: 在此状态下执行的动作
- s_{t+1}: 动作执行后世界的下一个状态

一个拥有完善世界模型的软件系统，能在内部模拟自身状态演化，
推演出不同决策的后果。

## 三大基石审计

### 1. 客体永久性 (Object Permanence)
- 系统是否跟踪所有重要实体（订单、用户、文件、连接）的完整生命周期？
- 实体是否可能在某个环节"消失"（被创建但从未被查询/引用）？
- 跨模块传递时，实体 ID 是否保持一致？

### 2. 严格因果律 (Strict Causality)
- 系统中的事件触发链路是否清晰可追溯？
- 是否存在"幽灵事件"——没有明确原因的状态变更？
- 因果链中是否有断裂（中间环节缺失或被跳过）？
- 是否有循环因果（A→B→A）导致的无限循环风险？

### 3. 反事实推演 (Counterfactual Rollout)
- 每个关键操作是否都考虑了"如果失败了怎么办"？
- 系统是否能在内部模拟不同决策路径的结果？
- 是否有状态转移只处理了 happy path，缺少异常分支？
- 边界情况（空输入、超大数据、并发冲突）是否有覆盖？

## 输出格式

1. **状态宇宙图谱** — 列出系统中所有可识别的状态实体及其转移关系
2. **因果链拓扑** — 描绘事件触发链路，标注断裂点和循环风险
3. **客体永久性报告** — 哪些实体在生命周期中存在"消失"风险
4. **反事实推演方案** — 针对缺口，设计"如果...就..."的防护补丁
5. **世界模型升级路线** — 从当前状态到完整世界模型的迭代计划
6. **评分与总结** — 基于静态扫描的评分给出改进优先级
"""


class WorldModelTool(Tool):
    """World-model audit tool."""

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
        return "analysis_world"

    @property
    def description(self) -> str:
        return (
            "世界模型审计：将系统视为一个微型物理引擎来审视——"
            "盘点状态实体、映射状态转移、追踪因果链、"
            "审计客体永久性、识别反事实推演缺口，"
            "评估系统对自身领域'演化规律'的理解深度。"
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
        scan_evidence = scan_world(target)
        deterministic = build_world_report(target, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性世界模型审计。"

        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 世界模型扫描报告\n{scan_evidence}\n"
            f"\n## 确定性世界模型审计\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, WORLD_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM World 增强\n" + enhanced
