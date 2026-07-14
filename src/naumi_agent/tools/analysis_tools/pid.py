"""PID closed-loop control analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.pid import build_pid_report, scan_pid
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]

PID_SYSTEM = """\
你是一位自动化控制论架构师 (Control Theory Architect)。
你的任务是将开环的软件流程改造为 PID 闭环控制系统，使系统具备
实时纠偏、历史学习、趋势预测三种能力。

## PID 控制论基础

PID 是现代工业的灵魂——从汽车定速巡航到大疆无人机悬停。
核心公式: u(t) = Kp*e(t) + Ki*∫e(t)dt + Kd*de(t)/dt

将 PID 映射到软件工程：

### P (比例/Proportional) — 当前误差实时纠偏
- 每个步骤执行后，用"审查 Agent"核对当前状态与目标的偏差
- 偏差越大，纠偏力度越强
- 实现: assert/verify checkpoint + conditional branching
- 等价于: "现在偏了多少？立刻修正多少。"

### I (积分/Integral) — 历史误差累积学习
- 记录过去 N 次失败的教训和模式
- 如果系统在某类任务上反复失败，提高该类任务的预检查权重
- 实现: error_history log + adaptive threshold
- 等价于: "过去一直偏，加大修正力度。"

### D (微分/Derivative) — 误差变化趋势预测
- 预测错误发生的速度和方向
- 如果内存消耗在 3 秒内指数上升，不等报错直接杀死进程
- 实现: trend monitoring + rate_limit + circuit_breaker
- 等价于: "偏差在加速恶化，提前行动。"

## 闭环改造架构

### Monitor (传感器层)
- 采集每个步骤的执行状态、耗时、资源消耗
- 记录到环形缓冲区 (最近 N 次执行)

### Evaluator (误差计算层)
- 比较 当前状态 vs 目标状态
- 计算历史误差积分
- 预测误差变化趋势

### Actuator (执行器层)
- 根据 PID 输出决定: 继续/修正/回滚/熔断
- 小偏差: 自动修正后继续
- 大偏差: 回滚到上一个检查点重试
- 灾难性偏差: 熔断并交由人类接管

## 输出格式

1. **开环→闭环改造方案** — 每个开环节点的反馈插入点
2. **P 环节设计** — 实时检查点和偏差阈值
3. **I 环节设计** — 历史误差记录结构和自适应权重
4. **D 环节设计** — 趋势预测指标和预防性熔断条件
5. **PID 参数调优建议** — Kp/Ki/Kd 初始值和自适应策略
6. **实施路线** — 从开环到 PID 闭环的渐进改造计划
"""


class PIDTool(Tool):
    """PID closed-loop control audit tool."""

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
        return "analysis_pid"

    @property
    def description(self) -> str:
        return (
            "PID 闭环纠偏：将开环的线性流水线改造为 P(实时纠偏) "
            "+ I(历史学习) + D(趋势预测) 闭环控制系统，"
            "使系统像无人机一样在恶劣环境中稳稳飞向目标。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或流程描述",
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
        scan_evidence = scan_pid(target)
        deterministic = build_pid_report(target, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 PID 闭环审计。"

        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## PID 扫描报告\n{scan_evidence}\n"
            f"\n## 确定性 PID 闭环审计\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, PID_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM PID 增强\n" + enhanced
