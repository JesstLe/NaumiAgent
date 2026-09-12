"""Shell hook 单元测试."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from naumi_agent.hooks.hook_manager import HookContext, HookManager, HookPoint
from naumi_agent.hooks.shell_hook import (
    _MAX_STDERR_BYTES,
    _MAX_STDOUT_BYTES,
    ShellHookConfig,
    _communicate_bounded,
    create_shell_hook_runner,
)
from naumi_agent.runtime.shell import create_shell_process


class TestShellHookConfig:
    def test_from_dict(self):
        cfg = ShellHookConfig.from_dict({"command": "echo hi", "timeout": 15})
        assert cfg.command == "echo hi"
        assert cfg.timeout == 15

    def test_from_dict_default_timeout(self):
        cfg = ShellHookConfig.from_dict({"command": "echo hi"})
        assert cfg.timeout == 10


class TestShellHookRunner:
    @pytest.mark.asyncio
    async def test_simple_command(self):
        cfg = ShellHookConfig(command="echo ok", timeout=5)
        runner = create_shell_hook_runner(cfg)
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_END, runner)

        ctx = HookContext(
            point=HookPoint.TOOL_EXECUTE_END,
            data={"tool_name": "test"},
        )
        await mgr.fire(ctx)

    @pytest.mark.asyncio
    async def test_receives_env_vars(self):
        cfg = ShellHookConfig(
            command="echo $NAUMI_HOOK_POINT $NAUMI_TOOL_NAME",
            timeout=5,
        )
        runner = create_shell_hook_runner(cfg)
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_START, runner)

        ctx = HookContext(
            point=HookPoint.TOOL_EXECUTE_START,
            data={"tool_name": "bash_run"},
        )
        await mgr.fire(ctx)

    @pytest.mark.asyncio
    async def test_receives_stdin_json(self):
        cfg = ShellHookConfig(
            command="cat",
            timeout=5,
        )
        runner = create_shell_hook_runner(cfg)
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_END, runner)

        ctx = HookContext(
            point=HookPoint.TOOL_EXECUTE_END,
            data={"tool_name": "file_read", "extra": 42},
        )
        await mgr.fire(ctx)

    @pytest.mark.asyncio
    async def test_abort_from_shell(self):
        cfg = ShellHookConfig(
            command='echo \'{"abort": true, "reason": "blocked"}\'',
            timeout=5,
        )
        runner = create_shell_hook_runner(cfg)
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_START, runner)

        ctx = HookContext(
            point=HookPoint.TOOL_EXECUTE_START,
            data={"tool_name": "rm_rf"},
        )
        result = await mgr.fire(ctx)
        assert result.should_abort
        assert result.data["abort_reason"] == "blocked"

    @pytest.mark.asyncio
    async def test_timeout(self):
        cfg = ShellHookConfig(command="sleep 60", timeout=1)
        runner = create_shell_hook_runner(cfg)
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_START, runner)

        ctx = HookContext(point=HookPoint.TOOL_EXECUTE_START)
        await mgr.fire(ctx)  # should not hang

    @pytest.mark.asyncio
    async def test_large_output_is_drained_with_bounded_memory(self, tmp_path: Path):
        script = tmp_path / "large_hook.py"
        script.write_text(
            "import sys\n"
            "sys.stdout.buffer.write(b'a' * 300_000)\n"
            "sys.stderr.buffer.write(b'b' * 300_000)\n",
            encoding="utf-8",
        )
        proc = await create_shell_process(
            f'{sys.executable} "{script}"',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stdout_bytes, stderr, stderr_bytes = await _communicate_bounded(
            proc,
            b"{}",
            timeout=10,
        )

        assert stdout_bytes == 300_000
        assert stderr_bytes == 300_000
        assert len(stdout) == _MAX_STDOUT_BYTES
        assert len(stderr) == _MAX_STDERR_BYTES
        assert stdout == b"a" * _MAX_STDOUT_BYTES
        assert stderr == b"b" * _MAX_STDERR_BYTES

    @pytest.mark.asyncio
    async def test_cancellation_reaps_shell_hook_process(self, monkeypatch):
        created = asyncio.Event()
        process = None

        async def recording_create(*args, **kwargs):
            nonlocal process
            process = await create_shell_process(*args, **kwargs)
            created.set()
            return process

        monkeypatch.setattr(
            "naumi_agent.hooks.shell_hook.create_shell_process",
            recording_create,
        )
        runner = create_shell_hook_runner(
            ShellHookConfig(command="sleep 60", timeout=30)
        )
        task = asyncio.create_task(
            runner(HookContext(point=HookPoint.TOOL_EXECUTE_START))
        )
        await asyncio.wait_for(created.wait(), timeout=5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert process is not None
        assert process.returncode is not None

    @pytest.mark.asyncio
    async def test_nonzero_exit_code(self):
        cfg = ShellHookConfig(command="exit 1", timeout=5)
        runner = create_shell_hook_runner(cfg)
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_END, runner)

        ctx = HookContext(point=HookPoint.TOOL_EXECUTE_END)
        await mgr.fire(ctx)  # should not crash

    @pytest.mark.asyncio
    async def test_coexists_with_python_hook(self):
        results = []

        # Python hook
        mgr = HookManager()

        @mgr.on(HookPoint.TOOL_EXECUTE_END)
        def python_hook(ctx):
            results.append("python")

        # Shell hook
        cfg = ShellHookConfig(command="echo shell_done", timeout=5)
        runner = create_shell_hook_runner(cfg)
        mgr.register(HookPoint.TOOL_EXECUTE_END, runner)

        ctx = HookContext(point=HookPoint.TOOL_EXECUTE_END)
        await mgr.fire(ctx)
        assert results == ["python"]

    @pytest.mark.asyncio
    async def test_shell_merges_extra_data(self):
        cfg = ShellHookConfig(
            command='echo \'{"custom_field": "hello"}\'',
            timeout=5,
        )
        runner = create_shell_hook_runner(cfg)
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_END, runner)

        ctx = HookContext(point=HookPoint.TOOL_EXECUTE_END)
        result = await mgr.fire(ctx)
        assert result.data.get("shell_custom_field") == "hello"

    @pytest.mark.asyncio
    async def test_file_path_env_var(self):
        cfg = ShellHookConfig(
            command="test -n \"$NAUMI_TOOL_FILE\" && echo has_file",
            timeout=5,
        )
        runner = create_shell_hook_runner(cfg)
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_START, runner)

        ctx = HookContext(
            point=HookPoint.TOOL_EXECUTE_START,
            data={"tool_name": "file_read", "file_path": "/tmp/test.py"},
        )
        await mgr.fire(ctx)
