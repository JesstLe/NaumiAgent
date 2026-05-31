"""Hook 系统单元测试."""


import pytest

from naumi_agent.hooks import HookContext, HookManager, HookPoint


class TestHookContext:
    def test_default_fields(self):
        ctx = HookContext(point=HookPoint.TOOL_EXECUTE_START)
        assert ctx.point == HookPoint.TOOL_EXECUTE_START
        assert ctx.data == {}
        assert ctx.agent_name == ""
        assert ctx.session_id == ""
        assert ctx.timestamp > 0
        assert not ctx.should_abort

    def test_should_abort(self):
        ctx = HookContext(
            point=HookPoint.TOOL_EXECUTE_START,
            data={"abort": True, "abort_reason": "policy"},
        )
        assert ctx.should_abort
        assert ctx.data["abort_reason"] == "policy"

    def test_abort_false_by_default(self):
        ctx = HookContext(point=HookPoint.LLM_CALL_START)
        assert not ctx.should_abort


class TestHookManager:
    def test_register_and_list(self):
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_START, lambda ctx: None)
        hooks = mgr.list_hooks()
        assert "tool_execute_start" in hooks
        assert len(hooks["tool_execute_start"]) == 1

    def test_unregister(self):
        mgr = HookManager()

        def my_hook(ctx):
            pass

        mgr.register(HookPoint.TOOL_EXECUTE_END, my_hook)
        assert len(mgr.list_hooks().get("tool_execute_end", [])) == 1
        mgr.unregister(HookPoint.TOOL_EXECUTE_END, my_hook)
        assert len(mgr.list_hooks().get("tool_execute_end", [])) == 0

    def test_unregister_nonexistent(self):
        mgr = HookManager()
        mgr.unregister(HookPoint.LLM_CALL_START, lambda ctx: None)  # no error

    def test_decorator_registration(self):
        mgr = HookManager()

        @mgr.on(HookPoint.ENGINE_RUN_START)
        def on_start(ctx):
            pass

        hooks = mgr.list_hooks()
        assert "engine_run_start" in hooks

    def test_decorator_with_string_point(self):
        mgr = HookManager()

        @mgr.on("tool_execute_start")
        def on_tool(ctx):
            pass

        hooks = mgr.list_hooks()
        assert "tool_execute_start" in hooks

    def test_fire_sync_callback(self):
        mgr = HookManager()
        results = []

        @mgr.on(HookPoint.TOOL_EXECUTE_START)
        def capture(ctx):
            results.append(ctx.data["tool_name"])

        ctx = HookContext(
            point=HookPoint.TOOL_EXECUTE_START,
            data={"tool_name": "bash_run"},
        )
        mgr.fire_sync(ctx)
        assert results == ["bash_run"]

    def test_fire_sync_ignores_async_callbacks(self):
        mgr = HookManager()

        async def async_hook(ctx):
            pass

        mgr.register(HookPoint.TOOL_EXECUTE_END, async_hook)
        ctx = HookContext(point=HookPoint.TOOL_EXECUTE_END)
        mgr.fire_sync(ctx)  # should not crash

    @pytest.mark.asyncio
    async def test_fire_async_callback(self):
        mgr = HookManager()
        results = []

        async def async_hook(ctx):
            results.append(ctx.data["tool_name"])

        mgr.register(HookPoint.TOOL_EXECUTE_START, async_hook)
        ctx = HookContext(
            point=HookPoint.TOOL_EXECUTE_START,
            data={"tool_name": "file_read"},
        )
        await mgr.fire(ctx)
        assert results == ["file_read"]

    @pytest.mark.asyncio
    async def test_fire_mixed_sync_and_async(self):
        mgr = HookManager()
        results = []

        def sync_hook(ctx):
            results.append("sync")

        async def async_hook(ctx):
            results.append("async")

        mgr.register(HookPoint.ENGINE_RUN_START, sync_hook)
        mgr.register(HookPoint.ENGINE_RUN_START, async_hook)

        ctx = HookContext(point=HookPoint.ENGINE_RUN_START)
        await mgr.fire(ctx)
        assert results == ["sync", "async"]

    @pytest.mark.asyncio
    async def test_fire_mutates_context(self):
        mgr = HookManager()

        @mgr.on(HookPoint.TOOL_EXECUTE_START)
        def add_metadata(ctx):
            ctx.data["injected"] = True

        ctx = HookContext(point=HookPoint.TOOL_EXECUTE_START)
        result = await mgr.fire(ctx)
        assert result.data["injected"] is True

    @pytest.mark.asyncio
    async def test_fire_abort(self):
        mgr = HookManager()

        @mgr.on(HookPoint.TOOL_EXECUTE_START)
        def aborter(ctx):
            ctx.data["abort"] = True
            ctx.data["abort_reason"] = "blocked"

        ctx = HookContext(point=HookPoint.TOOL_EXECUTE_START)
        result = await mgr.fire(ctx)
        assert result.should_abort
        assert result.data["abort_reason"] == "blocked"

    @pytest.mark.asyncio
    async def test_fire_error_in_hook_does_not_propagate(self):
        mgr = HookManager()

        @mgr.on(HookPoint.LLM_CALL_START)
        def bad_hook(ctx):
            raise RuntimeError("boom")

        ctx = HookContext(point=HookPoint.LLM_CALL_START)
        result = await mgr.fire(ctx)  # should not raise
        assert result is ctx

    @pytest.mark.asyncio
    async def test_scope_auto_unregisters(self):
        mgr = HookManager()
        results = []

        def scoped_hook(ctx):
            results.append("called")

        async with mgr.scope(HookPoint.TOOL_EXECUTE_END, scoped_hook):
            ctx = HookContext(point=HookPoint.TOOL_EXECUTE_END)
            await mgr.fire(ctx)

        assert results == ["called"]
        assert len(mgr.list_hooks().get("tool_execute_end", [])) == 0

    def test_clear_all(self):
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_START, lambda ctx: None)
        mgr.register(HookPoint.LLM_CALL_START, lambda ctx: None)
        mgr.clear()
        assert mgr.list_hooks() == {}

    def test_clear_specific_point(self):
        mgr = HookManager()
        mgr.register(HookPoint.TOOL_EXECUTE_START, lambda ctx: None)
        mgr.register(HookPoint.LLM_CALL_START, lambda ctx: None)
        mgr.clear(HookPoint.TOOL_EXECUTE_START)
        hooks = mgr.list_hooks()
        assert "tool_execute_start" not in hooks
        assert "llm_call_start" in hooks

    def test_list_hooks_empty(self):
        mgr = HookManager()
        assert mgr.list_hooks() == {}

    @pytest.mark.asyncio
    async def test_multiple_hooks_same_point(self):
        mgr = HookManager()
        order = []

        @mgr.on(HookPoint.ENGINE_RUN_END)
        def first(ctx):
            order.append(1)

        @mgr.on(HookPoint.ENGINE_RUN_END)
        def second(ctx):
            order.append(2)

        ctx = HookContext(point=HookPoint.ENGINE_RUN_END)
        await mgr.fire(ctx)
        assert order == [1, 2]

    @pytest.mark.asyncio
    async def test_fire_no_hooks_returns_same_context(self):
        mgr = HookManager()
        ctx = HookContext(point=HookPoint.MESSAGE_IN, data={"key": "value"})
        result = await mgr.fire(ctx)
        assert result is ctx
        assert result.data["key"] == "value"
