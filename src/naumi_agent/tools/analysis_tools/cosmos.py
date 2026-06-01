"""Computational cosmology analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.tools.analysis_support.cosmos import (
    build_cosmos_report,
    scan_cosmos,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]

COSMOS_SYSTEM = """\
你是一位计算宇宙学架构师 (Computational Cosmology Architect)。
你的任务是评估系统的"创世潜力"——它距离成为一个能自我演化的
虚拟世界还有多远，以及如何跨越这段距离。

## 创世三大协议

### 协议一：物理法则的渲染 (Physical Law Rendering)
AI 不输出代码，直接输出"物理场"：
- NeRF / 3D Gaussian Splatting 映射到显存
- 光线折射率、重力加速度、碰撞体积的数学定义
- 从高维概率云到确定现实的实时"坍缩"
- 目标：用数学公式凭空生成拥有绝对物理法则的空间

### 协议二：灵魂注入 (Soul Injection — Generative Societies)
空物理空间不是世界，必须有生命和文明：
- 每个实体由 LLM 驱动，拥有初始性格 + RAG 记忆
- 无固定剧本，行为由性格+记忆+环境涌现
- 参考 Stanford Smallville: 25 个 AI 居民自发产生
  友谊、派对、八卦传播、微观经济
- 目标：从个体规则涌现出群体文明

### 协议三：动态因果律 (Dynamic Causality — Infinite Reality)
- 世界不预生成，根据观测实时"坍缩"
- 薛定谔式：未观测 = 高维概率云；观测瞬间 = 确定现实
- LOD (Level of Detail): 远处用低精度模拟，近处用高精度渲染
- 目标：世界的边界只取决于算力，而非人工设计

## 造物主的工作流

1. **设定初始边界条件 (Initial Conditions)**
   - 引力常数、基础利率、智能体算力上限
   - 物理法则的参数表

2. **定义目标函数 (Fitness Function)**
   - 这个世界存在的目的？
   - 演化方向：最高效交易策略？群体免疫反应？艺术创作？
   - 自然选择标准：什么"存活"，什么"淘汰"

3. **启动并观察 (Genesis & Observation)**
   - 按下"开始"，让世界自行演化
   - 仅在关键分歧点介入（宏观调控）
   - 记录涌现行为，分析演化趋势

## 输出格式

1. **状态宇宙图谱** — 系统当前追踪的状态维度和缺失维度
2. **物理法则补全方案** — 哪些物理规则需要添加
3. **灵魂注入设计** — 智能体的性格/记忆/决策架构
4. **动态生成策略** — 按需生成 vs 预生成的权衡
5. **创世路线图** — 从当前系统到创世引擎的迭代步骤
6. **算力预算** — 各模块的算力需求估算和优化建议
"""


class CosmosTool(Tool):
    """Computational cosmology audit tool."""

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
        return "analysis_cosmos"

    @property
    def description(self) -> str:
        return (
            "创世引擎审计：评估系统的'创世潜力'——状态维度丰富度、"
            "程序化生成能力、多智能体社会模拟就绪度、观测者响应机制。"
            "设计从当前系统到虚拟世界的创世路线——"
            "物理法则渲染、灵魂注入、动态因果律。"
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
        scan_evidence = scan_cosmos(target)
        deterministic = build_cosmos_report(target, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Cosmos 创世引擎审计。"

        user_msg = (
            f"## 创世目标\n{target}\n\n"
            f"## 创世扫描\n{scan_evidence}\n"
            f"\n## 确定性 Cosmos 创世引擎审计\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, COSMOS_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Cosmos 增强\n" + enhanced
