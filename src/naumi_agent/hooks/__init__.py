"""Hook 系统 — 生命周期钩子，支持在 Agent 执行各阶段注入自定义逻辑.

两种注册方式：
1. Python 回调: @hooks.on(HookPoint.TOOL_EXECUTE_START)
2. Shell 命令: 在 config.yaml 的 hooks 段配置
"""

from naumi_agent.hooks.hook_manager import (
    HookContext,
    HookManager,
    HookPoint,
    HookTraceEntry,
)
from naumi_agent.hooks.shell_hook import ShellHookConfig, create_shell_hook_runner

__all__ = [
    "HookContext",
    "HookManager",
    "HookPoint",
    "HookTraceEntry",
    "ShellHookConfig",
    "create_shell_hook_runner",
]
