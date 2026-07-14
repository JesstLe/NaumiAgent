"""Adversarial self-play analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.spar import (
    build_spar_report,
    scan_spar,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]
SubagentManagerGetter = Callable[[ModelPort], Any | None]

SPAR_SYSTEM = """\
你是一位对抗性自博弈架构师 (Adversarial Self-Play Architect)。
你的任务是将 GAN（生成式对抗网络）思想应用于软件开发：设计一套
蓝军（写代码）vs 红军（搞破坏）的自动化对抗流水线。

## 核心架构

### 1. 蓝军 (The Builder)
- 目标：编写通过所有测试的功能代码
- 策略：从核心逻辑开始，逐步添加防御性代码
- 约束：不能通过"绕过"来满足测试，必须真正解决问题

### 2. 红军 (The Breaker)
- 目标：找到代码中的一切漏洞
- 策略：基于静态扫描发现的攻击面，生成极端测试输入
- 约束：攻击必须基于物理世界的真实威胁，不能虚无主义式地
  要求"绝对安全"

### 3. 物理锚点 (The Oracle)
- 所有验证必须基于真实执行结果，不能只靠 LLM "嘴炮"
- 代码必须在真实环境（容器/沙盒）中编译运行
- 使用 Valgrind/GDB/Sanitizer 等工具获取物理证据
- 核心转储 (core dump)、段错误 (segfault)、内存泄漏报告
  是不可伪造的物理判决

## 必须防止的两种绝症

### 绝症一：奖励作弊 (Reward Hacking)
蓝军发现捷径：加 if (size > 1GB) return "ok" 来"通过"大文件测试，
实际并未解决内存管理问题。

**对策：**
- 红军测试不能只看 return code，必须验证输出正确性
- 引入"功能完整性断言"：核心业务逻辑不能被跳过
- 检测"防御性短路"：异常处理中直接返回成功

### 绝症二：虚无主义 (Nihilism)
红军过于变态，蓝军为了安全把所有功能都删了。空代码零 Bug。

**对策：**
- 定义不可妥协的功能基线 (Functional Baseline)
- 每轮迭代必须有功能验收测试 (not just safety tests)
- 设置"功能保留率"指标，低于阈值视为虚无主义发作

## 自博弈流水线设计

### Round N:
1. **蓝军出击**: 基于当前代码 + 红军上轮反馈，编写修复/新功能
2. **编译验证**: 代码必须在真实环境编译通过 (Ground Truth #1)
3. **红军出击**: 基于扫描到的攻击面，生成极端输入并执行
4. **物理判决**: 执行结果由工具 (Valgrind/ASAN) 而非 LLM 判定
5. **收敛检查**: 功能完整性 ✅ + 零崩溃 ✅ + 无奖励作弊 ✅ → 终止

## 输出格式

1. **蓝军建设方案** — 需要编写的功能模块和防御性代码
2. **红军攻击策略** — 基于扫描发现的攻击面，生成具体测试方案
3. **物理沙盒配置** — Dockerfile/编译命令/Sanitizer 配置
4. **收敛准则** — 什么条件下停止迭代
5. **作弊防护** — 针对检测到的作弊风险，设计具体防护措施
6. **迭代预估** — 建议的迭代轮数和每轮重点
"""


class SparTool(Tool):
    """Adversarial self-play analysis tool."""

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
        return "analysis_spar"

    @property
    def description(self) -> str:
        return (
            "对抗性自博弈 (GAN for Code)：蓝军写代码 vs 红军搞破坏，"
            "以物理沙盒执行结果作为绝对锚点，迭代 N 轮直到代码坚不可摧。"
            "防止奖励作弊与虚无主义，交付真正经过对抗验证的代码。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要进行对抗自博弈的目标代码路径或功能描述",
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
        scan_evidence = scan_spar(task)
        deterministic = build_spar_report(task, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 SPAR 自博弈基线。"

        manager = self._subagent_manager_getter(router)
        if manager is not None:
            return await self._execute_adversarial(
                router,
                manager,
                task,
                scan_evidence,
            )

        user_msg = (
            f"## 对抗目标\n{task}\n\n"
            f"## 静态扫描报告\n{scan_evidence}\n"
            f"\n## 确定性 SPAR 基线\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, SPAR_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM SPAR 增强\n" + enhanced

    async def _execute_adversarial(
        self,
        router: ModelPort,
        manager: Any,
        task: str,
        scan_evidence: str,
    ) -> str:
        """Execute real adversarial self-play with blue/red agents + bus."""
        from naumi_agent.agents.base import AgentCapability
        from naumi_agent.agents.message_bus import (
            AgentMessage,
            MessagePriority,
        )
        from naumi_agent.orchestrator.subagent_manager import SubTask

        if self._run_analysis is None:
            return "模型路由未初始化，无法执行 SPAR 对抗总结。"

        spar_caps = [AgentCapability.FILE_OPS, AgentCapability.CODE_EXEC]

        await manager.message_bus.reset(
            preserve_blackboard_prefixes=("team/",),
            preserve_mailboxes=True,
        )

        await manager.message_bus.blackboard_set(
            "target",
            task,
            author="orchestrator",
        )
        await manager.message_bus.blackboard_set(
            "attack_surface",
            scan_evidence,
            author="orchestrator",
        )

        manager.spawn_for_task(
            name="spar_blue_builder",
            task_description=task,
            role="builder",
            focus="根据任务要求编写健壮的代码，防御已知的攻击向量",
            max_turns=5,
            max_budget_usd=0.2,
            extra_capabilities=spar_caps,
        )
        manager.spawn_for_task(
            name="spar_red_breaker",
            task_description=task,
            role="attacker",
            focus="审查蓝军编写的代码，找到所有可能的漏洞、边界问题和攻击面",
            max_turns=5,
            max_budget_usd=0.2,
            extra_capabilities=spar_caps,
        )

        rounds_log: list[str] = []
        blue_code = ""
        total_tokens = 0
        total_cost = 0.0
        max_rounds = 3

        try:
            for round_num in range(max_rounds):
                blue_task = f"## 对抗目标\n{task}\n\n## 静态扫描（攻击面）\n{scan_evidence}\n"
                if round_num > 0 and rounds_log:
                    blue_task += (
                        f"\n## 红军上轮攻击报告\n{rounds_log[-1]}\n"
                        "请修复上述所有漏洞，同时保持功能完整。"
                    )
                if blue_code:
                    blue_task += f"\n## 当前代码\n{blue_code[:10000]}\n"

                blue_subtask = SubTask(
                    id=f"blue_r{round_num}",
                    description=blue_task,
                    agent_name="spar_blue_builder",
                )
                blue_result = await manager.delegate(blue_subtask)
                total_tokens += getattr(blue_result, "total_tokens", 0)
                total_cost += getattr(blue_result, "total_cost_usd", 0.0)

                if blue_result.status != "completed":
                    rounds_log.append(f"⚠️ 蓝军第 {round_num + 1} 轮失败: {blue_result.error}")
                    break

                blue_code = blue_result.response or ""
                rounds_log.append(f"### 蓝军 第 {round_num + 1} 轮输出\n{blue_code[:3000]}")

                await manager.message_bus.blackboard_set(
                    "blue_code",
                    blue_code,
                    author="spar_blue_builder",
                )

                red_task = (
                    f"## 对抗目标\n{task}\n\n"
                    f"## 蓝军本轮代码\n{blue_code[:10000]}\n\n"
                    "请从以下角度全面攻击这段代码:\n"
                    "1. 边界条件（空输入、超大数据、特殊字符）\n"
                    "2. 并发/竞态条件\n"
                    "3. 资源泄漏（内存、文件句柄、连接）\n"
                    "4. 逻辑漏洞（未覆盖的分支、错误的条件）\n"
                    "5. 安全漏洞（注入、路径穿越、权限绕过）\n"
                )

                red_subtask = SubTask(
                    id=f"red_r{round_num}",
                    description=red_task,
                    agent_name="spar_red_breaker",
                )
                red_result = await manager.delegate(red_subtask)
                total_tokens += getattr(red_result, "total_tokens", 0)
                total_cost += getattr(red_result, "total_cost_usd", 0.0)

                if red_result.status != "completed":
                    rounds_log.append(f"⚠️ 红军第 {round_num + 1} 轮失败: {red_result.error}")
                    break

                attack_report = red_result.response or ""
                rounds_log.append(f"### 红军 第 {round_num + 1} 轮攻击报告\n{attack_report[:3000]}")

                await manager.message_bus.blackboard_set(
                    f"red_findings_r{round_num}",
                    attack_report[:2000],
                    author="spar_red_breaker",
                )

                has_critical = (
                    "CRITICAL" in attack_report.upper() or "HIGH" in attack_report.upper()
                )

                priority = MessagePriority.HIGH if has_critical else MessagePriority.LOW
                await manager.message_bus.send(
                    AgentMessage(
                        sender="spar_red_breaker",
                        topic="spar.attack_report",
                        recipient="spar_blue_builder",
                        content=attack_report[:500],
                        priority=priority,
                    )
                )

                if not has_critical:
                    rounds_log.append("✅ 红军未发现 CRITICAL/HIGH 级别漏洞，对抗训练收敛。")
                    break

        finally:
            manager.destroy("spar_blue_builder")
            manager.destroy("spar_red_breaker")

        bus_stats = manager.message_bus.stats()
        await manager.message_bus.reset(
            preserve_blackboard_prefixes=("team/",),
            preserve_mailboxes=True,
        )

        rounds_completed = len([r for r in rounds_log if "蓝军" in r and "输出" in r])
        synthesis_msg = (
            f"## 对抗自博弈 SPAR 报告\n\n"
            f"**目标**: {task[:200]}\n"
            f"**对抗轮次**: {rounds_completed}\n"
            f"**总 Token**: {total_tokens}\n"
            f"**总成本**: ${total_cost:.4f}\n"
            f"**消息总线**: {bus_stats['total_messages']} 条消息, "
            f"{bus_stats['blackboard_entries']} 条共享状态\n\n"
            f"---\n\n"
            f"## 对抗过程完整记录\n\n"
        )
        for entry in rounds_log:
            synthesis_msg += f"{entry}\n\n---\n\n"

        synthesis_msg += (
            "\n请基于上述对抗过程，给出最终的综合评估：\n"
            "1. 代码是否足够健壮？\n"
            "2. 残余风险有哪些？\n"
            "3. 推荐的后续加固措施？\n"
        )

        return await self._run_analysis(router, SPAR_SYSTEM, synthesis_msg)
