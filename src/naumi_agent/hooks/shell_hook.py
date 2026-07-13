"""Shell 命令 Hook — 通过配置文件注册外部命令作为生命周期钩子.

配置格式（config.yaml）::

    hooks:
      tool_execute_start:
        - command: "ruff check --fix $NAUMI_TOOL_FILE"
          timeout: 10
      tool_execute_end:
        - command: "notify-send 'Tool done'"
          timeout: 5
      engine_run_end:
        - command: "python3 scripts/post_run.py"
          timeout: 30

上下文通过环境变量 + stdin JSON 传递给子进程：
- 环境变量 NAUMI_HOOK_POINT, NAUMI_TOOL_NAME, NAUMI_AGENT_NAME, NAUMI_SESSION_ID
- stdin: HookContext 的完整 JSON（data 字段）
- stdout 中设置 abort=true 可中止操作（仅 TOOL_EXECUTE_START 有效）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from naumi_agent.hooks.hook_manager import HookContext
from naumi_agent.runtime.shell import create_shell_process, terminate_process_tree

logger = logging.getLogger(__name__)


@dataclass
class ShellHookConfig:
    """单条 shell hook 配置."""

    command: str
    timeout: int = 10

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShellHookConfig:
        return cls(
            command=data["command"],
            timeout=data.get("timeout", 10),
        )


@dataclass
class ShellHookEntry:
    """一个注册到 HookManager 的 shell hook 实例."""

    point: str
    config: ShellHookConfig


def create_shell_hook_runner(config: ShellHookConfig) -> Any:
    """为一条 shell 配置生成一个 async hook callback.

    回调接收 HookContext，执行子进程，并将 stdout 解析回 context。
    """
    async def runner(ctx: HookContext) -> None:
        proc: asyncio.subprocess.Process | None = None
        env = dict(os.environ)
        env["NAUMI_HOOK_POINT"] = ctx.point.value
        env["NAUMI_AGENT_NAME"] = ctx.agent_name
        env["NAUMI_SESSION_ID"] = ctx.session_id

        tool_name = ctx.data.get("tool_name", "")
        if tool_name:
            env["NAUMI_TOOL_NAME"] = tool_name

        # 文件路径（file_read/file_write/file_edit 等工具常用）
        file_path = ctx.data.get("file_path") or ctx.data.get("path", "")
        if file_path:
            env["NAUMI_TOOL_FILE"] = str(file_path)

        stdin_payload = json.dumps(
            {"point": ctx.point.value, "data": ctx.data, "agent_name": ctx.agent_name},
            ensure_ascii=False,
        )

        try:
            proc = await create_shell_process(
                config.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_payload.encode()),
                timeout=config.timeout,
            )
        except TimeoutError:
            if proc is not None:
                await terminate_process_tree(proc)
            logger.warning(
                "Shell hook timed out (%ds): %s", config.timeout, config.command,
            )
            return
        except Exception:
            logger.exception("Shell hook failed: %s", config.command)
            return

        if proc.returncode and proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            logger.warning(
                "Shell hook exited %d: %s stderr=%s",
                proc.returncode, config.command, stderr_text,
            )

        # 解析 stdout，支持子进程控制 abort
        stdout_text = stdout.decode(errors="replace").strip()
        if stdout_text:
            _parse_shell_output(stdout_text, ctx)

    runner._shell_hook_config = config  # type: ignore[attr-defined]
    return runner


def _parse_shell_output(stdout_text: str, ctx: HookContext) -> None:
    """解析子进程 stdout，支持 abort 控制.

    格式：第一行若是 JSON 且包含 {"abort": true}，则标记中止。
    """
    first_line = stdout_text.split("\n", 1)[0].strip()
    if not first_line:
        return

    try:
        result = json.loads(first_line)
    except json.JSONDecodeError:
        return

    if isinstance(result, dict):
        if result.get("abort"):
            ctx.data["abort"] = True
            ctx.data["abort_reason"] = result.get("reason", "blocked by shell hook")
        for key in ("abort", "reason"):
            if key in result:
                continue
            # 合并其他字段到 data
        for key, value in result.items():
            if key not in ("abort", "reason"):
                ctx.data[f"shell_{key}"] = value
