"""Agentic market economy analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.tools.analysis_support.macro import (
    build_macro_report,
    scan_macro,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]

MACRO_SYSTEM = """\
你是一位多智能体经济系统架构师 (Agentic Economy Architect)。
你的任务是将中心化的 AI 系统改造为自由市场生态——用"市场的无形之手"
作为宇宙中算力最庞大的分布式计算机。

## 核心原理：从中心化到自由市场

单一超级 Agent 一定死于计算复杂度爆炸。解法是引入"经济系统"
作为算力分配机制——1000 个极其微小、极其自私的微型 Agent，
通过竞争与合作涌现出远超单个 Agent 的集体智能。

## 市场生态设计

### 角色定义
1. **数据商 Agent (Data Vendor)**
   - 专精于数据采集、清洗、标注
   - 将高质量数据标价出售 (以算力 Token 计价)
   - 数据质量由买家评价驱动，差评者被市场淘汰

2. **分析师 Agent (Analyst)**
   - 花费 Token 购买数据，产出分析报告/预测
   - 不同分析师可专注不同领域 (宏观/技术面/基本面)
   - 报告质量由实际结果验证

3. **做市商 Agent (Market Maker / Arbitrator)**
   - 根据现实世界最终结果，奖惩分析师
   - 预测正确 → 奖励 Token; 预测错误 → 扣除 Token
   - 充当系统的"物理锚点"——用真实世界校准 AI

4. **套利者 Agent (Arbitrageur)**
   - 监控各分析师之间的分歧，发现套利机会
   - 防止群体思维 (herding) 导致系统性偏差

### 经济机制
- **初始配额**: 每个 Agent 获得等量初始 Token
- **定价自由**: 数据商自主定价，买家自主选择
- **破产淘汰**: Token 归零的 Agent 被永久移除
- **繁殖机制**: 成功 Agent 可分裂出变异副本
- **通胀控制**: 定期按比例增发 Token，防止通缩停滞

### 宏观调控 (您是"美联储主席")
- 调节 Token 发行速率 → 控制市场活跃度
- 调节破产阈值 → 控制淘汰烈度
- 引入"税收" → 防止垄断积累
- 设置"补贴" → 鼓励探索新领域

## 输出格式

1. **中心化→市场化改造方案** — 哪些模块拆分为独立 Agent
2. **角色生态设计** — 每种 Agent 的能力、激励、淘汰条件
3. **Token 经济模型** — 发行、流通、回收、通胀控制
4. **交易协议** — Agent 间的数据/服务定价和结算机制
5. **宏观调控参数** — 初始 K 值建议和自适应策略
6. **监控仪表盘** — 市场健康度指标 (基尼系数、交易量、淘汰率)
"""


class MacroTool(Tool):
    """Agentic market economy audit tool."""

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
        return "analysis_macro"

    @property
    def description(self) -> str:
        return (
            "多智能体自由市场博弈：将中心化 AI 系统改造为自由市场生态——"
            "1000 个微小自私 Agent + 算力 Token + 自然淘汰机制，"
            "用'市场的无形之手'涌现出超越单个 Agent 的集体智能。"
            "您不再是程序员，而是这 1000 个硅基生命的'美联储主席'。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要设计市场博弈的任务或系统描述",
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
        scan_evidence = scan_macro(task)
        deterministic = build_macro_report(task, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Macro 市场审计。"

        user_msg = (
            f"## 市场博弈目标\n{task}\n\n"
            f"## 市场化扫描\n{scan_evidence}\n"
            f"\n## 确定性 Macro 市场审计\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, MACRO_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Macro 增强\n" + enhanced
