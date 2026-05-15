"""代码沙箱 — Docker 容器内执行."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any

from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)


class CodeExecuteTool(Tool):
    """在 Docker 沙箱中执行代码."""

    @property
    def name(self) -> str:
        return "code_execute"

    @property
    def description(self) -> str:
        return (
            "在隔离的 Docker 容器中执行代码。"
            "支持 python、javascript、bash。自动安装依赖。"
            "如果 Docker 不可用，则在本地临时目录执行。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的代码"},
                "language": {
                    "type": "string",
                    "description": "编程语言：python | javascript | bash",
                    "default": "python",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时（秒），默认 30",
                    "default": 30,
                },
            },
            "required": ["code"],
        }

    async def execute(
        self, *, code: str, language: str = "python", timeout: int = 30, **kwargs: Any
    ) -> str:
        if await self._docker_available():
            return await self._run_in_docker(code, language, timeout)
        return await self._run_local(code, language, timeout)

    async def _docker_available(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
            return proc.returncode == 0
        except (TimeoutError, FileNotFoundError):
            return False

    async def _run_in_docker(self, code: str, language: str, timeout: int) -> str:
        image_map = {
            "python": "python:3.12-slim",
            "javascript": "node:20-slim",
            "bash": "ubuntu:22.04",
        }
        image = image_map.get(language, "python:3.12-slim")

        # 写入临时文件
        ext_map = {"python": ".py", "javascript": ".js", "bash": ".sh"}
        ext = ext_map.get(language, ".txt")

        with tempfile.NamedTemporaryFile(mode="w", suffix=ext, delete=False, encoding="utf-8") as f:
            f.write(code)
            host_path = f.name

        try:
            container_path = f"/tmp/code{ext}"
            cmd_map = {
                "python": ["python", container_path],
                "javascript": ["node", container_path],
                "bash": ["bash", container_path],
            }
            cmd = cmd_map.get(language, ["python", container_path])

            proc = await asyncio.create_subprocess_exec(
                "docker",
                "run",
                "--rm",
                "-v",
                f"{host_path}:{container_path}:ro",
                "--network",
                "none",
                "--memory",
                "256m",
                "--cpus",
                "1",
                image,
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")

            parts = []
            if out.strip():
                parts.append(out)
            if err.strip():
                parts.append(f"[stderr]\n{err}")
            if proc.returncode != 0:
                parts.append(f"[exit code: {proc.returncode}]")

            return "\n".join(parts) if parts else "(no output)"
        except TimeoutError:
            return f"Error: Execution timed out after {timeout}s"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"
        finally:
            os.unlink(host_path)

    async def _run_local(self, code: str, language: str, timeout: int) -> str:
        """Docker 不可用时，在本地临时目录执行（降级方案）."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ext_map = {"python": ".py", "javascript": ".js", "bash": ".sh"}
            ext = ext_map.get(language, ".txt")
            file_path = os.path.join(tmpdir, f"code{ext}")

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)

            cmd_map = {
                "python": ["python3", file_path],
                "javascript": ["node", file_path],
                "bash": ["bash", file_path],
            }
            cmd = cmd_map.get(language, ["python3", file_path])

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tmpdir,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                out = stdout.decode("utf-8", errors="replace")
                err = stderr.decode("utf-8", errors="replace")

                parts = []
                if out.strip():
                    parts.append(out)
                if err.strip():
                    parts.append(f"[stderr]\n{err}")
                if proc.returncode != 0:
                    parts.append(f"[exit code: {proc.returncode}]")

                return "\n".join(parts) if parts else "(no output)"
            except TimeoutError:
                return f"Error: Execution timed out after {timeout}s"
            except Exception as e:
                return f"Error: {type(e).__name__}: {e}"


def create_sandbox_tools() -> list[Tool]:
    return [CodeExecuteTool()]
