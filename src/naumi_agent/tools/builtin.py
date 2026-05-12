"""内置工具：文件读写、编辑、命令执行."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from typing import Any

from naumi_agent.tools.base import Tool


class FileReadTool(Tool):
    """读取文件内容."""

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return "读取指定路径的文件内容。支持 offset 和 limit 参数读取部分内容。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始行号（从 0 开始），默认 0",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取的行数，默认读取全部",
                    "default": -1,
                },
            },
            "required": ["path"],
        }

    async def execute(self, *, path: str, offset: int = 0, limit: int = -1, **kwargs: Any) -> str:
        resolved = os.path.expanduser(path)
        if not os.path.isfile(resolved):
            return f"Error: File not found: {path}"

        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total = len(lines)
            end = total if limit < 0 else min(offset + limit, total)
            selected = lines[offset:end]

            result = "".join(selected)
            header = f"File: {path} ({total} lines"
            if offset > 0 or limit > 0:
                header += f", showing lines {offset + 1}-{end}"
            header += ")\n"

            return header + result
        except Exception as e:
            return f"Error reading file: {type(e).__name__}: {e}"


class FileWriteTool(Tool):
    """写入文件."""

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return "将内容写入指定文件。如果文件不存在则创建，存在则覆盖。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要写入的文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的内容",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, *, path: str, content: str, **kwargs: Any) -> str:
        resolved = os.path.expanduser(path)

        try:
            os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully wrote {len(content)} characters to {path}"
        except Exception as e:
            return f"Error writing file: {type(e).__name__}: {e}"


class FileEditTool(Tool):
    """编辑文件 — 搜索替换."""

    @property
    def name(self) -> str:
        return "file_edit"

    @property
    def description(self) -> str:
        return (
            "对文件进行精确的搜索替换编辑。"
            "提供 old_text 和 new_text，将文件中首次出现的 old_text 替换为 new_text。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径",
                },
                "old_text": {
                    "type": "string",
                    "description": "要被替换的原始文本",
                },
                "new_text": {
                    "type": "string",
                    "description": "替换后的新文本",
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(self, *, path: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        resolved = os.path.expanduser(path)

        if not os.path.isfile(resolved):
            return f"Error: File not found: {path}"

        try:
            with open(resolved, "r", encoding="utf-8") as f:
                content = f.read()

            count = content.count(old_text)
            if count == 0:
                return f"Error: old_text not found in {path}"
            if count > 1:
                return f"Error: old_text appears {count} times in {path}. Please provide more context to make it unique."

            new_content = content.replace(old_text, new_text, 1)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(new_content)

            return f"Successfully edited {path} (replaced 1 occurrence)"
        except Exception as e:
            return f"Error editing file: {type(e).__name__}: {e}"


class BashRunTool(Tool):
    """执行 shell 命令."""

    @property
    def name(self) -> str:
        return "bash_run"

    @property
    def description(self) -> str:
        return (
            "在 shell 中执行命令并返回输出。"
            "支持超时设置。工作目录默认为项目根目录。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（秒），默认 30",
                    "default": 30,
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录，默认为当前目录",
                },
            },
            "required": ["command"],
        }

    async def execute(self, *, command: str, timeout: int = 30, cwd: str | None = None, **kwargs: Any) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            output_parts = []
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                output_parts.append(f"[stderr]\n{stderr.decode('utf-8', errors='replace')}")

            output = "\n".join(output_parts) if output_parts else "(no output)"

            if proc.returncode != 0:
                output += f"\n[exit code: {proc.returncode}]"

            # 截断过长输出
            if len(output) > 50000:
                output = output[:50000] + f"\n... (truncated, {len(output)} total chars)"

            return output
        except asyncio.TimeoutError:
            return f"Error: Command timed out after {timeout}s"
        except Exception as e:
            return f"Error executing command: {type(e).__name__}: {e}"


def create_builtin_tools() -> list[Tool]:
    """创建所有内置工具实例."""
    return [FileReadTool(), FileWriteTool(), FileEditTool(), BashRunTool()]
