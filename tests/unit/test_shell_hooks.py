"""Shell hook 单元测试."""


import pytest

from naumi_agent.hooks.hook_manager import HookContext, HookManager, HookPoint
from naumi_agent.hooks.shell_hook import ShellHookConfig, create_shell_hook_runner


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
