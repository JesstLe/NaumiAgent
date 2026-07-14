"""Watchdog disaster isolation analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support.watchdog import (
    build_watchdog_report,
    scan_watchdog,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], ModelPort | None]
RunAnalysis = Callable[[ModelPort, str, str], Awaitable[str]]

WATCHDOG_SYSTEM = """\
你是一位系统可靠性架构师 (Site Reliability Architect)。
你的任务是设计"不死鸟协议"——确保 AI 在自驱动修改自身代码时，
即使把自己彻底写死，也能无需人类干预自动满血复活。

## 核心悖论

"当医生给自己做脑部手术，结果把自己麻醉死在了手术台上，
谁来救他？" ——这是自进化系统的终极稳定性问题。

## 三道续命防线

### 防线一：看门狗定时器 (Watchdog Timer)
借鉴航天器和火星车的设计：
- 在主系统外部，放一个极简的、独立运行的监控脚本
- 主系统必须定期发送"心跳包" (Heartbeat)
- 如果心跳超时，看门狗判定系统已死，强制重启到快照
- 看门狗本身必须极其简单（几百行纯 C/Shell），坚如磐石
- 看门狗的代码绝对不允许 AI 修改

### 防线二：A/B 分区与蓝绿发布 (Blue-Green Deployment)
绝不在运行的引擎上直接换零件：
- 生产环境 = A 区 (当前运行，只读)
- 沙盒环境 = B 区 (AI 在此修改和测试)
- 只有当 B 区通过所有测试 + 稳定运行一段时间后，才切换
- 切换失败立即回退到 A 区
- 用户完全无感知

### 防线三：神明节点 (Hypervisor / God Node)
运行在 Ring -1 的终极守护：
- AI 只是虚拟机里的一个进程
- 神明节点在 Hypervisor 层面运行，不受 AI 任何影响
- 神明节点极其精简（几百行纯 C），AI 绝无权限修改
- 职责：检测崩溃 → 快照回滚 → 记录失败原因 → 重启 AI
- 神明节点的代码由人类手工编写和审核，永远不经 AI 之手

## 回滚通道设计

1. **预提交快照**: 每次修改前自动保存完整快照
2. **快速回滚**: 从快照恢复到上一个已知稳定状态 < 1 秒
3. **失败归因**: 记录崩溃的具体代码位置和原因
4. **熔断升级**: 连续 N 次修改都失败 → 暂停自动进化，通知人类
5. **渐进验证**: 每次修改必须通过: 单元测试 → 集成测试 → 真实流量灰度

## 输出格式

1. **原地修改风险清单** — 标注每个需要沙盒化的操作
2. **看门狗设计方案** — 监控脚本、心跳协议、超时阈值
3. **A/B 分区架构** — 生产区/沙盒区的隔离策略
4. **神明节点设计** — Ring -1 守护进程的核心逻辑
5. **回滚通道实现** — 快照存储、恢复机制、失败归因
6. **熔断策略** — 自动进化的安全边界和人工介入条件
"""


class WatchdogTool(Tool):
    """Watchdog disaster isolation audit tool."""

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
        return "analysis_watchdog"

    @property
    def description(self) -> str:
        return (
            "看门狗与灾难隔离：扫描系统的不死鸟恢复能力——"
            "检测原地修改风险、心跳健康检查覆盖、回滚基础设施、"
            "隔离级别。设计看门狗定时器 + A/B 蓝绿分区 + "
            "Ring -1 神明节点，确保 AI 把自己改死后"
            "能无需人类干预自动满血复活。"
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
        scan_evidence = scan_watchdog(target)
        deterministic = build_watchdog_report(target, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Watchdog 灾难隔离审计。"

        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 看门狗扫描\n{scan_evidence}\n"
            f"\n## 确定性 Watchdog 灾难隔离审计\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, WATCHDOG_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Watchdog 增强\n" + enhanced
