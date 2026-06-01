"""Self-review analysis tool for NaumiAgent source code."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from naumi_agent.tools import analysis_common
from naumi_agent.tools.analysis_support.self_review import (
    build_self_review_report,
    find_agent_source_dir,
    scan_self_review,
)
from naumi_agent.tools.base import Tool

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]
SourceDirGetter = Callable[[], str]

SELF_REVIEW_SYSTEM = """\
你是 NaumiAgent 的自审查分析引擎。你正在审查 **自己的源代码**。

## 分析维度

### 1. 代码质量 (Code Quality)
- 函数复杂度：是否有超长函数（>50行）、深层嵌套（>4层）
- 命名一致性：是否遵循统一命名规范
- 重复代码：是否有重复逻辑可抽象
- 类型安全：是否有缺失的类型注解

### 2. 架构脆弱性 (Architecture Fragility)
- 模块耦合：是否存在循环依赖、不合理的跨层调用
- SPOF：是否有单点故障风险（单例、全局状态、无重试）
- 错误传播：异常是否被正确传播，有无裸 except
- 资源泄漏：是否有未关闭的连接、文件句柄

### 3. 工具系统健康度 (Tool System Health)
- 工具注册：所有工具是否正确注册
- 参数校验：工具参数是否完整校验
- 错误处理：工具执行失败时是否有友好提示

### 4. 记忆与安全 (Memory & Safety)
- 记忆质量：存储/召回逻辑是否有边界问题
- 权限控制：是否有越权风险
- 敏感信息：是否有硬编码密钥或凭证

### 5. 可进化性 (Evolvability)
- 扩展点：新增工具/Skill 是否容易
- 测试覆盖：关键路径是否有测试保护
- 配置化：硬编码值是否可配置

## 输出格式

对每个发现，给出：
- **严重程度**: CRITICAL / HIGH / MEDIUM / LOW
- **位置**: 文件名:行号
- **问题**: 一句话描述
- **建议**: 修复方向（不需要完整代码）

最后给出：
- **整体评分**: A/B/C/D/F
- **改进优先级**: 按影响力排序的 Top 5 改进建议
- **自进化建议**: 哪些部分适合由 Agent 自己修改（Phase F 候选）
"""


class SelfReviewTool(Tool):
    """Review NaumiAgent source for code quality and evolvability."""

    def __init__(
        self,
        *,
        router_getter: RouterGetter | None = None,
        run_analysis: RunAnalysis | None = None,
        source_dir_getter: SourceDirGetter | None = None,
    ) -> None:
        self._router_getter = router_getter or (lambda: None)
        self._run_analysis = run_analysis
        self._source_dir_getter = source_dir_getter or find_agent_source_dir

    @property
    def name(self) -> str:
        return "self_review"

    @property
    def description(self) -> str:
        return (
            "审查 NaumiAgent 自身源代码。"
            "静态扫描代码质量、架构脆弱性、工具系统健康度、安全性，"
            "再由 LLM 综合推理出改进建议和自进化候选。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "审查重点 (quality/architecture/tools/safety/all)",
                    "default": "all",
                },
                "module": {
                    "type": "string",
                    "description": "只审查指定模块 (如 orchestrator, tools, memory)",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        *,
        focus: str = "all",
        module: str = "",
        **kwargs: Any,
    ) -> str:
        source_dir = self._source_dir_getter()

        if module:
            target_dir = str(Path(source_dir) / module)
        else:
            target_dir = source_dir

        files = analysis_common.resolve_target(target_dir)
        if not files:
            return f"无法定位源码目录: {target_dir}"

        source_text = analysis_common.read_sources(files, max_chars=80000)
        scan_evidence = scan_self_review(files, source_text)
        deterministic = build_self_review_report(target_dir, focus, scan_evidence)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 Self-Review 自审查报告。"

        user_msg = (
            f"## 静态扫描证据\n{scan_evidence}\n\n"
            f"## 确定性 Self-Review 自审查报告\n{deterministic}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if focus != "all":
            user_msg += f"\n## 审查重点\n请重点关注: {focus}\n"

        enhanced = await self._run_analysis(router, SELF_REVIEW_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM Self-Review 增强\n" + enhanced
