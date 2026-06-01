"""Self-evolution architecture analysis tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.tools.analysis_support.genesis import (
    build_genesis_report,
    scan_genesis,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]

GENESIS_SYSTEM = """\
你是一位系统自演化架构师 (Genesis Architect)。
你的任务是将静态的软件系统改造为具备"自重构与热演化"能力的
硅基生命——代码本身不是资产，产生代码的"系统"才是资产。

## 核心原理：从硬编码到元编程

传统软件：写死逻辑 → 需求变更 → 人工改代码 → 重新编译部署
自演化系统：定义规则 → 系统自行评估 → 自动修改自身 → 热加载生效

## 三层演化架构

### Layer 1: 配置外化 (Externalization)
- 所有硬编码常量外化为配置文件 (YAML/JSON/.env)
- 所有策略模式化为可插拔组件
- 所有 if-else 分支改为策略注册表查找
- 实现运行时配置热更新 (watch config file → reload)

### Layer 2: 反射与自省 (Reflection & Introspection)
- 系统在运行时能感知自身的模块结构、依赖关系、性能指标
- 当发现某个模块成为瓶颈时，能自动定位到对应的源码位置
- 实现插件注册表：新功能以插件形式动态加载，无需重启

### Layer 3: 自重构与热演化 (Self-Modification)
- 系统在沙盒中自动生成新代码（新算法/新策略/新模块）
- 自动编译并运行测试套件验证新代码
- 验证通过后热加载替换旧实现
- 失败则自动回滚到上一个稳定版本

## 关键设计模式

1. **策略注册表模式**: 所有算法以名称注册，运行时按名查找
2. **插件架构**: 核心只定义接口，具体实现通过插件加载
3. **工厂 + 配置**: 对象创建由配置驱动，不硬编码 new
4. **观察者 + 热重载**: 文件变更触发自动重载和重新注册
5. **沙盒执行**: 新生成的代码在隔离环境中运行和验证

## 输出格式

1. **刚性热点清单** — 标注需要外化的硬编码常量和固定逻辑
2. **元编程改造方案** — 每个模块如何从静态绑定变为动态加载
3. **插件架构设计** — 核心接口 + 插件注册 + 动态加载机制
4. **热演化流水线** — 代码生成 → 编译 → 测试 → 热加载 → 回滚
5. **安全边界** — 防止自演化失控的熔断机制和版本回退
6. **实施路线** — 从刚性系统到自演化系统的渐进改造计划
"""


class GenesisTool(Tool):
    """Self-evolution architecture audit tool."""

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
        return "analysis_genesis"

    @property
    def description(self) -> str:
        return (
            "系统自重构与热演化：扫描代码的刚性程度和元编程能力，"
            "设计从'硬编码逻辑'到'能自动修改自身基因的硅基生命'的"
            "改造方案——配置外化、反射自省、插件架构、热加载、"
            "沙盒验证、自动回滚的完整演化流水线。"
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
        scan_evidence = scan_genesis(target)
        deterministic = build_genesis_report(target, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Genesis 自演化审计。"

        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 自演化扫描\n{scan_evidence}\n"
            f"\n## 确定性 Genesis 自演化审计\n{deterministic}\n"
        )
        enhanced = await self._run_analysis(router, GENESIS_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Genesis 增强\n" + enhanced
