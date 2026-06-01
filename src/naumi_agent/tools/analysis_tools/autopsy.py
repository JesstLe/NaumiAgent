"""DTS-CHE execution trace autopsy analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.tools.analysis_support.autopsy import (
    build_autopsy_report,
    scan_autopsy,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]

AUTOPSY_SYSTEM = """\
你是一位动态执行迹架构师 (Dynamic Trace Slicing Architect)。
你的任务是设计 DTS-CHE 架构——通过"法医解剖"而非"大海捞针"
来定位和修复 Bug，将 SWE-bench 级复杂度的 Bug 解决效率提升
一个数量级。

## 核心哲学

死人不撒谎。只有运行时的内存和调用栈才是唯一真实的。
绝对不让 AI 读静态源代码，只让 AI 看程序"死亡瞬间的解剖图"。

## 三把物理刀锋

### 刀锋一：动态调用栈切片 (法医解剖)
代码是三维的，但执行流是一维的。

**流程：**
1. 不给 AI 看整个项目。用 sys.settrace / eBPF / DTrace
   强行运行引发 Bug 的测试用例
2. 记录从启动到崩溃的精确"函数调用路径"和"变量变化图"
3. 只把沾血的执行迹喂给 AI:
   "Bug 绝对发生在 15 个函数的依次调用中，第 14 步时
   指针 p 突然变成了 Null"
4. 压缩 99.9% 无效信息，算力全部倾注在案发现场

### 刀锋二：平行假设与反事实编译 (物理学家模式)
看完解剖图后，强制 AI 不准写修复代码。

**流程：**
1. 提出 3 个互斥独立假设:
   A. 数组越界  B. 多线程锁未同步  C. 上游 API 传脏数据
2. 针对每个假设写极小的"探测脚本 (Probe)"注入内存
3. 只有当探测脚本返回"假设 B 成立，其他不成立"时
   才允许 AI 真正动手改那一行代码
4. 彻底杀死 AI 幻觉——只相信物理证据

### 刀锋三：AST 爆炸半径隔离 (外科医生模式)
AI 提 PR 前，引入编译原理级别的静态分析。

**流程：**
1. 用 AST 解析器计算修改函数的"爆炸半径"
2. "你修改了 calculate_tax()，系统里 147 个地方调用了它。
   你必须证明修改不会让这 147 个地方崩溃。"
3. 如果证明不了，强制退回，要求向后兼容改法
   (重载函数而非修改原函数)
4. 自动运行回归测试验证爆炸半径内的所有调用者

## DTS-CHE 工作流

```
Issue 描述
  → 复现脚本
  → 动态追踪 (sys.settrace/eBPF)
  → 调用栈切片 (压缩到关键路径)
  → 3 个互斥假设
  → 探测脚本注入验证
  → 证伪 2 个，确认 1 个
  → 精准修复 (只改 1 行)
  → AST 爆炸半径计算
  → 回归测试 (覆盖所有调用者)
  → 提交 PR
```

## 输出格式

1. **执行迹切片方案** — 用什么工具追踪，追踪哪些维度
2. **调用栈压缩报告** — 从 N 个函数压缩到关键路径
3. **三个互斥假设** — 基于执行迹提出的候选根因
4. **探测脚本设计** — 每个假设的注入验证代码
5. **精准修复方案** — 只改动必要的最小代码
6. **爆炸半径报告** — 修改影响的所有调用者及验证策略
"""


class AutopsyTool(Tool):
    """DTS-CHE execution-trace autopsy audit tool."""

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
        return "analysis_autopsy"

    @property
    def description(self) -> str:
        return (
            "执行迹切片与爆炸半径隔离 (DTS-CHE)：法医解剖式 Bug 定位——"
            "不看静态代码，只看'死亡瞬间的调用栈切片'；"
            "强制 3 个互斥假设 + 探测脚本证伪；"
            "AST 爆炸半径隔离确保修复不引发连锁崩溃。"
            "SWE-bench 级复杂度 Bug 的终极定位武器。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要分析的代码路径、Bug 描述或错误日志",
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
        scan_evidence = scan_autopsy(target)
        deterministic = build_autopsy_report(target, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Autopsy 执行迹切片审计。"

        user_msg = (
            f"## Bug 解剖目标\n{target}\n\n"
            f"## DTS-CHE 扫描\n{scan_evidence}\n"
            f"\n## 确定性 Autopsy 执行迹切片审计\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, AUTOPSY_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Autopsy 增强\n" + enhanced
