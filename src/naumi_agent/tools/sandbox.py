"""代码沙箱 — Docker 容器内执行，降级到受限本地执行."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from typing import Any

from naumi_agent.tools.base import Tool, ToolMetadata

logger = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 100_000
MAX_CODE_CHARS = 100_000
MIN_EXEC_TIMEOUT_SECONDS = 1
MAX_EXEC_TIMEOUT_SECONDS = 60
SUPPORTED_LANGUAGES = frozenset({"python", "javascript", "bash"})

# Cache docker availability to avoid repeated checks.
_docker_available_cache: bool | None = None


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
            "如果 Docker 不可用，则在本地临时目录执行（受限模式）。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            command_argument_names=("code",),
            user_facing_name="代码执行",
            search_hint="execute code sandbox docker python javascript bash",
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
        self,
        *,
        code: str,
        language: str = "python",
        timeout: int = 30,
        **kwargs: Any,
    ) -> str:
        try:
            code, language, timeout = _normalize_execution_inputs(
                code,
                language,
                timeout,
            )
        except ValueError as e:
            return f"代码执行已拒绝: {e}"

        if await self._check_docker():
            return await self._run_in_docker(code, language, timeout)
        return await self._run_local(code, language, timeout)

    async def _check_docker(self) -> bool:
        global _docker_available_cache
        if _docker_available_cache is not None:
            return _docker_available_cache

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
            _docker_available_cache = proc.returncode == 0
        except TimeoutError:
            if proc is not None:
                await _kill_process(proc)
            _docker_available_cache = False
        except FileNotFoundError:
            _docker_available_cache = False

        return _docker_available_cache

    async def _run_in_docker(self, code: str, language: str, timeout: int) -> str:
        image_map = {
            "python": "python:3.12-slim",
            "javascript": "node:20-slim",
            "bash": "ubuntu:22.04",
        }
        image = image_map.get(language, "python:3.12-slim")

        ext_map = {"python": ".py", "javascript": ".js", "bash": ".sh"}
        ext = ext_map.get(language, ".txt")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=ext, delete=False, encoding="utf-8",
        ) as f:
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

            try:
                stdout, stdout_bytes, stderr, stderr_bytes = await _communicate_bounded(
                    proc,
                    timeout=timeout,
                )
            except TimeoutError:
                return f"Error: Execution timed out after {timeout}s"

            out = _decode_bounded(stdout, total_bytes=stdout_bytes)
            err = _decode_bounded(stderr, total_bytes=stderr_bytes)

            parts: list[str] = []
            if out.strip():
                parts.append(out)
            if err.strip():
                parts.append(f"[stderr]\n{err}")
            if proc.returncode != 0:
                parts.append(f"[exit code: {proc.returncode}]")

            return "\n".join(parts) if parts else "(no output)"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"
        finally:
            os.unlink(host_path)

    async def _run_local(self, code: str, language: str, timeout: int) -> str:
        """Docker 不可用时，在本地临时目录执行（受限降级方案）."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ext_map = {"python": ".py", "javascript": ".js", "bash": ".sh"}
            ext = ext_map.get(language, ".txt")
            file_path = os.path.join(tmpdir, f"code{ext}")

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)

            cmd_map = {
                "python": [sys.executable, file_path],
                "javascript": ["node", file_path],
                "bash": ["bash", file_path],
            }
            cmd = cmd_map.get(language, [sys.executable, file_path])

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tmpdir,
                )

                try:
                    stdout, stdout_bytes, stderr, stderr_bytes = (
                        await _communicate_bounded(proc, timeout=timeout)
                    )
                except TimeoutError:
                    return (
                        f"Error: Execution timed out after {timeout}s "
                        "(本地模式，无资源隔离)"
                    )

                out = _decode_bounded(stdout, total_bytes=stdout_bytes)
                err = _decode_bounded(stderr, total_bytes=stderr_bytes)

                parts: list[str] = []
                if out.strip():
                    parts.append(out)
                if err.strip():
                    parts.append(f"[stderr]\n{err}")
                if proc.returncode != 0:
                    parts.append(f"[exit code: {proc.returncode}]")

                return "\n".join(parts) if parts else "(no output)"
            except Exception as e:
                return f"Error: {type(e).__name__}: {e}"


async def _kill_process(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess, best-effort."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


async def _communicate_bounded(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
) -> tuple[bytes, int, bytes, int]:
    """Drain both output streams while retaining only bounded previews."""
    if proc.stdout is None or proc.stderr is None:
        await _kill_process(proc)
        raise RuntimeError("代码执行子进程缺少输出管道。")

    stdout_task = asyncio.create_task(_read_bounded(proc.stdout))
    stderr_task = asyncio.create_task(_read_bounded(proc.stderr))
    wait_task = asyncio.create_task(proc.wait())
    completion = asyncio.gather(wait_task, stdout_task, stderr_task)
    try:
        await asyncio.wait_for(
            asyncio.shield(completion),
            timeout=timeout,
        )
    except TimeoutError:
        await _kill_process(proc)
        await completion
        raise
    except asyncio.CancelledError:
        await _kill_process(proc)
        await completion
        raise
    stdout, stdout_bytes = stdout_task.result()
    stderr, stderr_bytes = stderr_task.result()
    return stdout, stdout_bytes, stderr, stderr_bytes


async def _read_bounded(
    stream: asyncio.StreamReader,
    *,
    max_bytes: int = _MAX_OUTPUT_BYTES,
) -> tuple[bytes, int]:
    retained = bytearray()
    total_bytes = 0
    while chunk := await stream.read(64 * 1024):
        total_bytes += len(chunk)
        remaining = max_bytes - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
    return bytes(retained), total_bytes


def _decode_bounded(data: bytes, *, total_bytes: int) -> str:
    text = data.decode("utf-8", errors="replace")
    if total_bytes <= len(data):
        return text
    return text + f"\n... (输出已截断，原始大小 {total_bytes} 字节)"


def _truncate(text: str, max_bytes: int = _MAX_OUTPUT_BYTES) -> str:
    """Truncate text if it exceeds max_bytes."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return truncated + f"\n... (输出已截断，原始大小 {len(encoded)} 字节)"


def _normalize_execution_inputs(
    code: Any,
    language: Any,
    timeout: Any,
) -> tuple[str, str, int]:
    """Validate public code execution inputs before spawning processes."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code 不能为空，且必须是字符串。")
    if len(code) > MAX_CODE_CHARS:
        raise ValueError(f"code 过长，当前上限为 {MAX_CODE_CHARS} 个字符。")

    if not isinstance(language, str):
        raise ValueError("language 必须是字符串。")
    normalized_language = language.strip().lower()
    if normalized_language not in SUPPORTED_LANGUAGES:
        allowed = " | ".join(sorted(SUPPORTED_LANGUAGES))
        raise ValueError(f"language 只能是: {allowed}。")

    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValueError("timeout 必须是整数秒。")
    if timeout < MIN_EXEC_TIMEOUT_SECONDS or timeout > MAX_EXEC_TIMEOUT_SECONDS:
        raise ValueError(
            "timeout 必须在 "
            f"{MIN_EXEC_TIMEOUT_SECONDS} 到 {MAX_EXEC_TIMEOUT_SECONDS} 秒之间。"
        )

    return code, normalized_language, timeout


def create_sandbox_tools() -> list[Tool]:
    return [CodeExecuteTool()]
