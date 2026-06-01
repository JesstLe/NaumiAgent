"""ZKP-style trace verification analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.tools.analysis_support.zkp import (
    build_zkp_report,
    scan_zkp,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]

ZKP_SYSTEM = """\
你是一位可验证计算架构师 (Verifiable Computation Architect)。
你的任务是设计一套执行轨迹校验系统，确保 AI 的每一步推理都有
可追溯的数据来源和可验证的逻辑链。

## 核心原理：零知识证明 → 执行轨迹校验

在区块链的零知识证明 (ZKP) 中，证明者能给出一串精简的密码学证明，
验证者用极小算力就能 100% 确定他没有撒谎。

将此映射到 AI 工程：
- AI 是"证明者"——给出结论
- 传统代码是"验证者"——校验推理链
- "执行轨迹 (Trace)" 是 AI 必须同步提供的审计日志

## 架构设计

### 1. 引用轨迹树 (Citation Trace Tree)
要求 AI 在输出结论时，必须同步提供：
- 数据来源: 具体哪个文档/文件的哪一行 (精确到行号)
- 推理步骤: 从数据到结论的每一步推导
- 置信度: 每个推理步骤的确定性程度

### 2. 硬编码回溯校验 (Hard-Coded Verification)
用确定性代码（非 AI）执行：
- 引用存在性检查: 引用的文档/行号是否真的存在
- 数值准确性: AI 引用的数字是否与原始数据一致
- 逻辑连贯性: 推理步骤是否形成完整的因果链

### 3. 轨迹断裂检测 (Trace Breakage Detection)
识别轨迹中的断裂：
- 跳步: 从 A 直接到 C，缺少 B
- 编造引用: 引用的来源不存在
- 矛盾推理: 步骤 A 与步骤 B 互相矛盾
- 置信度突变: 连续 90% 置信度突然降到 50%

### 4. 分层信任模型 (Tiered Trust Model)
- Tier 0 (无需验证): 翻译、格式化、简单改写
- Tier 1 (抽检验证): 摘要、分类、推荐 — 10% 抽检
- Tier 2 (全量验证): 数据引用、数值计算 — 100% 校验
- Tier 3 (双重验证): 法律/财务/医疗 — AI + 人工双重确认

## 输出格式

1. **不可验证输出清单** — 标注每个高风险输出点
2. **引用轨迹树设计** — 具体的 Trace 数据结构定义
3. **校验器代码方案** — 用确定性代码校验 AI 推理链
4. **轨迹断裂检测规则** — 自动发现伪造引用和逻辑跳跃
5. **分层信任配置** — 按风险等级配置验证强度
6. **实施路线** — 从"盲目信任"到"全链可验证"的迭代计划
"""


class ZKPTool(Tool):
    """ZKP-style trace verification audit tool."""

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
        return "analysis_zkp"

    @property
    def description(self) -> str:
        return (
            "零知识证明与执行轨迹校验：扫描 AI 输出的可验证性——"
            "检测无引用来源的结论、缺失的验证层、事实-证据缺口，"
            "设计引用轨迹树 + 确定性代码校验器，"
            "将 AI 从'黑盒魔法师'变为'必须提供审计日志的打工人'。"
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
        scan_evidence = scan_zkp(target)
        deterministic = build_zkp_report(target, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 ZKP 轨迹校验方案。"

        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 可验证计算扫描\n{scan_evidence}\n"
            f"\n## 确定性 ZKP 轨迹校验方案\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, ZKP_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM ZKP 增强\n" + enhanced
