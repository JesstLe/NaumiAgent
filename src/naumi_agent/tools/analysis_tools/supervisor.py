"""Erlang-style supervisor tree analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from naumi_agent.tools.analysis_support.supervisor import (
    build_supervisor_report,
    scan_supervisor,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]
SubagentManagerGetter = Callable[[Any], Any | None]

SUPERVISOR_SYSTEM = """\
你是一位 Erlang/OTP 守护者架构师 (Supervisor Tree Architect)。
你的任务是设计"双子星架构"——进化 Agent 拓荒创新 + 守护 Agent
兜底复活，践行 "Let it crash" 工程哲学。

## 核心理念：Let it Crash

不要试图写一个永远不出错的完美 Agent。
设计一个"允许出错，但能瞬间复活"的系统架构。

## 三大不对称铁律

### 铁律一：智商不对称 (The Genius vs. The Janitor)
- **进化 Agent (Worker)**:
  高智商、大参数、发散思维 (Temperature=0.8)
  负责创新、写代码、试错。极不稳定，随时可能崩。
- **守护 Agent (Supervisor)**:
  极低智商、极简代码、死板状态机 (Temperature=0.0)
  可能只有几百行纯 C/Python 的 if-else。
  脑子里只有: if (死了) { 重启 }

### 铁律二：权限不对称 (Ring 3 vs Ring 0)
- **进化 Agent**: 沙盒 Ring 3，只能改自己的业务代码
- **守护 Agent**: Ring 0 上帝权限，握着电源线和快照备份
- 进化 Agent 一旦死锁，守护 Agent 直接切断电源，无需商量

### 铁律三：回滚优先于调试 (Rollback over Debugging)
- 守护 Agent 提取崩溃日志 → 回滚到上一个稳定版本
  → 把报错日志甩给进化 Agent："你写的代码崩了，日志在这，重写"

## 架构设计

### Supervisor Tree 结构
Supervisor (Ring 0, 简单状态机)
  Worker_1 (LLM 创意生成)
  Worker_2 (数据抓取/解析)
  Worker_3 (代码编译/测试)
  Supervisor_2 (子守护者)
    Worker_4 (交易策略执行)
    Worker_5 (风控计算)

### 重启策略
1. 永久型 (Permanent): 崩溃立即重启，不休不眠
2. 临时型 (Transient): 正常退出不重启，异常退出才重启
3. 临时工 (Temporary): 崩了就崩了，不自动重启

### 故障升级
- Worker 崩溃 → Supervisor 重启 Worker (N 次)
- Worker 连续崩溃 N 次 → Supervisor 认为任务有毒
- Supervisor 向上级 Supervisor 报告 → 可能需要人类介入
- 根 Supervisor 连续失败 → 触发全系统熔断，等待人类

## 输出格式

1. **Worker 清单** — 每个高风险模块的守护需求等级
2. **Supervisor Tree 设计** — 完整的守护者树层级结构
3. **重启策略配置** — 每个 Worker 的重启策略和阈值
4. **权限隔离方案** — Ring 0/Ring 3 权限分配
5. **故障升级链路** — 从 Worker 崩溃到人类介入的升级路径
6. **控制流图** — 完整的双子星架构控制流
"""


@dataclass(frozen=True)
class _SupervisorAgentOutcome:
    status: str
    response: str
    error: str
    total_tokens: int = 0
    total_cost_usd: float = 0.0


class SupervisorTool(Tool):
    """Erlang-style supervisor tree audit tool."""

    def __init__(
        self,
        *,
        router_getter: RouterGetter | None = None,
        run_analysis: RunAnalysis | None = None,
        subagent_manager_getter: SubagentManagerGetter | None = None,
    ) -> None:
        self._router_getter = router_getter or (lambda: None)
        self._run_analysis = run_analysis
        self._subagent_manager_getter = subagent_manager_getter or (lambda _router: None)

    @property
    def name(self) -> str:
        return "analysis_supervisor"

    @property
    def description(self) -> str:
        return (
            "Erlang 守护者与 Let-it-crash 协议：设计'进化 Agent 拓荒 + "
            "守护 Agent 兜底'的双子星架构——智商不对称、权限不对称、"
            "回滚优先于调试。Worker 崩了由 Supervisor 自动回滚重启。"
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
        scan_evidence = scan_supervisor(target)
        deterministic = build_supervisor_report(target, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Supervisor 守护者树审计。"

        manager = self._subagent_manager_getter(router)
        if manager is not None:
            enhanced = await self._execute_with_supervisor_tree(
                router,
                manager,
                target,
                scan_evidence,
            )
            return deterministic + "\n\n## 多智能体 Supervisor 增强\n" + enhanced

        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 守护者树扫描\n{scan_evidence}\n"
            f"\n## 确定性 Supervisor 守护者树审计\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, SUPERVISOR_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Supervisor 增强\n" + enhanced

    async def _execute_with_supervisor_tree(
        self,
        router: Any,
        manager: Any,
        target: str,
        scan_evidence: str,
    ) -> str:
        """Execute real supervisor tree pattern with worker + guardian agents."""
        from naumi_agent.agents.base import AgentCapability
        from naumi_agent.orchestrator.subagent_manager import SubTask

        worker_result: Any = _SupervisorAgentOutcome(
            status="failed",
            response="",
            error="Worker 尚未执行。",
        )
        guardian_result: Any = _SupervisorAgentOutcome(
            status="failed",
            response="",
            error="Guardian 尚未执行。",
        )
        manager.spawn_for_task(
            name="supervisor_worker",
            task_description=target,
            role="worker",
            focus="分析目标代码的崩溃点和恢复策略",
            model_tier="fast",
            max_turns=5,
            max_budget_usd=0.15,
            extra_capabilities=[
                AgentCapability.FILE_OPS,
                AgentCapability.CODE_EXEC,
            ],
        )
        manager.spawn_for_task(
            name="supervisor_guardian",
            task_description=target,
            role="guardian",
            focus="权限不对称、回滚优先于调试、隔离爆炸半径",
            model_tier="capable",
            max_turns=3,
            max_budget_usd=0.15,
        )

        total_tokens = 0
        total_cost = 0.0
        crash_points = ""

        try:
            worker_task = f"分析以下目标的崩溃点:\n\n{target}\n\n## 静态扫描结果\n{scan_evidence}\n"

            worker_subtask = SubTask(
                id="worker_analysis",
                description=worker_task,
                agent_name="supervisor_worker",
            )
            worker_result = await manager.delegate(worker_subtask)
            total_tokens += getattr(worker_result, "total_tokens", 0)
            total_cost += getattr(worker_result, "total_cost_usd", 0.0)

            if worker_result.status == "completed":
                crash_points = worker_result.response or ""
            else:
                crash_points = (
                    "⚠️ Worker 节点崩溃 (Let-it-crash!): "
                    f"{worker_result.error or '未知错误'}\n\n"
                    "这正是 Erlang 哲学的体现——Worker 崩溃是正常的，"
                    "Guardian 会兜底分析。"
                )

            guardian_task = (
                f"## 审计目标\n{target}\n\n"
                f"## Worker 崩溃分析\n{crash_points}\n\n"
                f"## 静态扫描\n{scan_evidence}\n\n"
                "基于以上信息，设计完整的 Supervisor 树:\n"
                "1. 树形层级结构（Supervisor → Worker）\n"
                "2. 每层重启策略\n"
                "3. 回滚点定义\n"
                "4. 爆炸半径隔离方案\n"
                "5. 心跳和健康检查设计\n"
            )
            guardian_subtask = SubTask(
                id="guardian_design",
                description=guardian_task,
                agent_name="supervisor_guardian",
            )
            guardian_result = await manager.delegate(guardian_subtask)
            total_tokens += getattr(guardian_result, "total_tokens", 0)
            total_cost += getattr(guardian_result, "total_cost_usd", 0.0)

        finally:
            manager.destroy("supervisor_worker")
            manager.destroy("supervisor_guardian")

        worker_status = (
            "✅ Worker 正常完成"
            if worker_result.status == "completed"
            else f"⚠️ Worker 崩溃 (Let-it-crash): {worker_result.error}"
        )
        guardian_status = (
            "✅ Guardian 设计完成"
            if guardian_result.status == "completed"
            else f"⚠️ Guardian 异常: {guardian_result.error}"
        )

        report = (
            f"## Erlang 守护者树分析报告\n\n"
            f"**目标**: {target[:200]}\n"
            f"**Worker 状态**: {worker_status}\n"
            f"**Guardian 状态**: {guardian_status}\n"
            f"**总 Token**: {total_tokens}\n"
            f"**总成本**: ${total_cost:.4f}\n\n"
            f"---\n\n"
            f"### Worker 崩溃点分析\n{crash_points}\n\n---\n\n"
            f"### Guardian 守护者树设计\n"
        )
        if guardian_result.status == "completed":
            report += guardian_result.response
        else:
            report += f"Guardian 异常: {guardian_result.error}"

        return report
