# Browser Tool Reliability and Replay Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `browser_waitFor` 与 `browser_evaluate` 的真实 Playwright 故障，并让 trace、managed 视频和 attached screencast 默认全部关闭且只能显式开启。

**Architecture:** 保留现有工具与 `BrowserRuntime` 边界，在运行时内部修正 Playwright 调用语义；回放策略由 `BrowserAutomationConfig` 进入共享 runtime，再传播到 TaskRunner 的隔离 runtime。三个行为切片严格独立走 TDD、真实 Chromium 验证和单独提交。

**Tech Stack:** Python 3.14、async Playwright、Pydantic Settings、pytest/pytest-asyncio、ruff。

## Global Constraints

- 所有用户可见文案、错误提示使用中文；代码注释使用英文；commit message 使用英文。
- 一个缺陷一个提交；完成当前缺陷的验证与自审后才能开始下一个。
- 每个缺陷必须先看到回归测试按预期失败，再写生产代码。
- 每个提交执行 `ruff check src/`、import smoke、相关 pytest 和真实 Chromium 场景。
- 最终执行 `.venv/bin/python -m pytest tests/ -x`；现有无关 E2E 基线失败必须如实记录，不混入本计划修复。
- 不改变网页自身结构或跳转，不调整 compaction，不删除既有 artifacts。

---

### Task 1: 修复 `browser_waitFor` 的关键字参数调用

**Files:**
- Modify: `src/naumi_agent/tools/browser/runtime/browser_runtime.py:2530-2608`
- Test: `tests/unit/test_browser_runtime.py`

**Interfaces:**
- Consumes: `BrowserRuntime.wait_for(*, text, text_gone, selector, timeout) -> dict[str, Any]`
- Produces: 对 `Page.wait_for_function(expression, *, arg=..., timeout=..., polling=...)` 的兼容调用；返回契约保持不变。

- [ ] **Step 1: 写入文本出现与文本消失的失败测试**

在 `tests/unit/test_browser_runtime.py` 的 runtime 测试区域增加：

```python
@pytest.mark.asyncio
async def test_wait_for_passes_text_as_keyword_argument(
    tmp_path: Path,
) -> None:
    runtime = BrowserRuntime(tmp_path)
    runtime.browser = MagicMock()
    runtime.page = MagicMock()
    runtime.page.wait_for_function = AsyncMock()
    runtime._capture_step_screenshot = AsyncMock(
        return_value=str(tmp_path / "wait-text.png")
    )

    result = await runtime.wait_for(text="Welcome", timeout=5000)

    call = runtime.page.wait_for_function.await_args
    assert call is not None
    assert len(call.args) == 1
    assert call.kwargs["arg"] == "Welcome"
    assert call.kwargs["timeout"] == 5000
    assert call.kwargs["polling"] == 500
    assert result["matched"] == "text"


@pytest.mark.asyncio
async def test_wait_for_passes_text_gone_as_keyword_argument(
    tmp_path: Path,
) -> None:
    runtime = BrowserRuntime(tmp_path)
    runtime.browser = MagicMock()
    runtime.page = MagicMock()
    runtime.page.wait_for_function = AsyncMock()
    runtime._capture_step_screenshot = AsyncMock(
        return_value=str(tmp_path / "wait-gone.png")
    )

    result = await runtime.wait_for(text_gone="Loading", timeout=6000)

    call = runtime.page.wait_for_function.await_args
    assert call is not None
    assert len(call.args) == 1
    assert call.kwargs["arg"] == "Loading"
    assert call.kwargs["timeout"] == 6000
    assert result["matched"] == "textGone"
```

- [ ] **Step 2: 运行测试并确认按正确原因失败**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_browser_runtime.py::test_wait_for_passes_text_as_keyword_argument \
  tests/unit/test_browser_runtime.py::test_wait_for_passes_text_gone_as_keyword_argument -vv
```

Expected: 两个测试都 FAIL，显示 `len(call.args) == 2` 或 `arg` 不在关键字参数中；不得是 fixture/import 错误。

- [ ] **Step 3: 最小修复两个 Playwright 调用点**

在 `BrowserRuntime.wait_for()` 中做以下两个精确替换：

```diff
-                    text,
+                    arg=text,
```

```diff
-                    text_gone,
+                    arg=text_gone,
```

其余 JavaScript、timeout 与 polling 参数不变。

- [ ] **Step 4: 运行单元回归与浏览器工具相关测试**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_browser_runtime.py tests/unit/test_browser_tools.py -q
```

Expected: PASS，且无新的 warning/error。

- [ ] **Step 5: 用真实 Chromium 验证文本出现、文本消失和超时**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio
import tempfile
from pathlib import Path

from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="naumi-wait-") as raw:
        root = Path(raw)
        page = root / "page.html"
        page.write_text(
            "<title>Wait Test</title><body><h1>Welcome</h1></body>",
            encoding="utf-8",
        )
        runtime = BrowserRuntime(root / "runtime")
        try:
            await runtime.start({"source": "managed", "headless": True})
            await runtime.goto(page.as_uri())
            appeared = await runtime.wait_for(text="Welcome", timeout=1500)
            gone = await runtime.wait_for(text_gone="Loading", timeout=1500)
            missing = await runtime.wait_for(text="Never Appears", timeout=1000)
            assert appeared["matched"] == "text", appeared
            assert gone["matched"] == "textGone", gone
            assert missing["timedOut"] is True, missing
            assert "positional arguments" not in missing.get("error", "")
        finally:
            await runtime.stop()


asyncio.run(main())
PY
```

Expected: exit 0；真实超时来自 Playwright timeout，不再出现 positional arguments 参数错误。

- [ ] **Step 6: 运行提交门禁并提交**

Run:

```bash
ruff check src/
.venv/bin/python -c "from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime"
git diff --check
git add src/naumi_agent/tools/browser/runtime/browser_runtime.py tests/unit/test_browser_runtime.py
git commit -m "fix(browser): pass wait arguments by keyword"
```

Expected: 检查通过，只提交 Task 1 的生产代码和测试。

---

### Task 2: 修复 `browser_evaluate` 的表达式返回值

**Files:**
- Modify: `src/naumi_agent/tools/browser/runtime/browser_runtime.py:2297-2320`
- Test: `tests/unit/test_browser_runtime.py`

**Interfaces:**
- Consumes: `BrowserRuntime.evaluate(expression: str) -> dict[str, Any]`
- Produces: 原始表达式直接传给 `Page.evaluate()`；现有 `{result, isError}` 契约和 8 KiB 上限不变。

- [ ] **Step 1: 写入原始表达式传递的失败测试**

```python
@pytest.mark.asyncio
async def test_evaluate_passes_raw_expression_and_returns_value(
    tmp_path: Path,
) -> None:
    runtime = BrowserRuntime(tmp_path)
    runtime.browser = MagicMock()
    runtime.page = MagicMock()
    runtime.page.evaluate = AsyncMock(return_value="Naumi Repro")

    result = await runtime.evaluate("document.title")

    runtime.page.evaluate.assert_awaited_once_with("document.title")
    assert result == {"result": "Naumi Repro", "isError": False}
```

- [ ] **Step 2: 运行测试并确认表达式被包装导致失败**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_browser_runtime.py::test_evaluate_passes_raw_expression_and_returns_value -vv
```

Expected: FAIL，实际调用参数为 `(() => { document.title })()`，而不是 `document.title`。

- [ ] **Step 3: 直接把原始表达式交给 Playwright**

将当前包装调用替换为：

```python
result = await self.page.evaluate(expression)
```

保留现有异常捕获、JSON 序列化、空值处理、8 KiB 截断和 artifact 事件记录。

- [ ] **Step 4: 增加对象、空值、异常与截断的回归测试**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_result", "serialized"),
    [
        ({"count": 2}, '{\n  "count": 2\n}'),
        (None, ""),
        ("x" * 9000, "x" * 8192),
    ],
)
async def test_evaluate_serializes_supported_results(
    tmp_path: Path,
    page_result: object,
    serialized: str,
) -> None:
    runtime = BrowserRuntime(tmp_path)
    runtime.browser = MagicMock()
    runtime.page = MagicMock()
    runtime.page.evaluate = AsyncMock(return_value=page_result)

    result = await runtime.evaluate("window.__value")

    assert result == {"result": serialized, "isError": False}


@pytest.mark.asyncio
async def test_evaluate_returns_page_error(
    tmp_path: Path,
) -> None:
    runtime = BrowserRuntime(tmp_path)
    runtime.browser = MagicMock()
    runtime.page = MagicMock()
    runtime.page.evaluate = AsyncMock(side_effect=RuntimeError("page closed"))

    result = await runtime.evaluate("document.title")

    assert result == {"result": "page closed", "isError": True}
```

- [ ] **Step 5: 运行单元回归与真实 Chromium 求值**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_browser_runtime.py tests/unit/test_browser_tools.py -q
.venv/bin/python - <<'PY'
import asyncio
import tempfile
from pathlib import Path

from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime
from naumi_agent.tools.browser.tools import BrowserEvaluateTool


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="naumi-evaluate-") as raw:
        root = Path(raw)
        page = root / "page.html"
        page.write_text(
            "<title>Naumi Evaluate</title><body></body>",
            encoding="utf-8",
        )
        runtime = BrowserRuntime(root / "runtime")
        try:
            await runtime.start({"source": "managed", "headless": True})
            await runtime.goto(page.as_uri())
            output = await BrowserEvaluateTool(runtime).execute(
                expression="document.title"
            )
            assert output == "Naumi Evaluate", output
        finally:
            await runtime.stop()


asyncio.run(main())
PY
```

Expected: pytest PASS，真实工具输出精确等于 `Naumi Evaluate`。

- [ ] **Step 6: 运行提交门禁并提交**

```bash
ruff check src/
.venv/bin/python -c "from naumi_agent.tools.browser.tools import BrowserEvaluateTool"
git diff --check
git add src/naumi_agent/tools/browser/runtime/browser_runtime.py tests/unit/test_browser_runtime.py
git commit -m "fix(browser): return JavaScript evaluation results"
```

Expected: 只提交 Task 2 的生产代码和测试。

---

### Task 3: 默认关闭所有浏览器回放录制

**Files:**
- Modify: `src/naumi_agent/config/settings.py:239-245`
- Modify: `src/naumi_agent/orchestrator/engine.py:561-563,1072-1085`
- Modify: `src/naumi_agent/tools/browser/runtime/browser_runtime.py:534-579,1042-1060,1118-1139,3560-3588`
- Modify: `src/naumi_agent/tools/browser/orchestrator/task_runner.py:327-409`
- Modify: `src/naumi_agent/tools/browser/tools.py:1320-1332`
- Test: `tests/unit/test_config.py`
- Test: `tests/unit/test_browser_runtime.py`
- Test: `tests/unit/test_task_runner.py`
- Test: `tests/unit/test_engine.py`
- Test: `tests/unit/test_browser_tools.py`

**Interfaces:**
- Consumes: `BrowserAutomationConfig.replay_recording_enabled: bool`
- Produces: `BrowserRuntime(base_dir, *, replay_recording_enabled=False)`；TaskRunner 的共享与隔离 runtime 使用相同策略；显式 `true` 保持原回放能力。

- [ ] **Step 1: 写入配置默认值、YAML 与环境变量的失败测试**

在 `tests/unit/test_config.py` 增加：

```python
def test_browser_replay_recording_is_disabled_by_default() -> None:
    assert AppConfig().browser.replay_recording_enabled is False


def test_browser_replay_recording_can_be_enabled_from_yaml(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "browser:\n  replay_recording_enabled: true\n",
        encoding="utf-8",
    )

    config = AppConfig.from_yaml(config_path)

    assert config.browser.replay_recording_enabled is True


def test_browser_replay_recording_loads_from_nested_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NAUMI_BROWSER__REPLAY_RECORDING_ENABLED",
        "true",
    )

    assert AppConfig().browser.replay_recording_enabled is True
```

- [ ] **Step 2: 运行配置测试并确认属性缺失**

```bash
.venv/bin/python -m pytest tests/unit/test_config.py -k replay_recording -vv
```

Expected: FAIL，原因是 `BrowserAutomationConfig` 尚无 `replay_recording_enabled`。

- [ ] **Step 3: 增加默认关闭的配置字段与 runtime 构造参数**

在 `BrowserAutomationConfig` 增加：

```python
replay_recording_enabled: bool = False
```

将 runtime 构造函数改为：

```python
def __init__(
    self,
    base_dir: str | Path,
    *,
    replay_recording_enabled: bool = False,
) -> None:
    self.base_dir = Path(base_dir)
    self.replay_recording_enabled = replay_recording_enabled
```

`reset_runtime_state()` 不修改该策略字段。

- [ ] **Step 4: 写入 managed/attached 默认关闭与显式开启的失败测试**

先在 `TestBrowserRuntimeInit` 前增加完整的测试组装函数：

```python
def _managed_runtime_fixture(
    tmp_path: Path,
    *,
    replay_recording_enabled: bool = False,
) -> tuple[BrowserRuntime, MagicMock, MagicMock]:
    runtime = BrowserRuntime(
        tmp_path,
        replay_recording_enabled=replay_recording_enabled,
    )
    runtime.artifacts.start_session()
    fake_page = MagicMock()
    fake_context = MagicMock()
    fake_context.tracing.start = AsyncMock()
    fake_context.new_page = AsyncMock(return_value=fake_page)
    fake_context.add_init_script = AsyncMock()
    fake_browser = MagicMock()
    fake_browser.new_context = AsyncMock(return_value=fake_context)
    fake_chromium = MagicMock()
    fake_chromium.launch = AsyncMock(return_value=fake_browser)
    runtime._playwright = MagicMock(chromium=fake_chromium)
    return runtime, fake_browser, fake_context


def _attached_runtime_fixture(
    tmp_path: Path,
    *,
    replay_recording_enabled: bool = False,
) -> tuple[BrowserRuntime, MagicMock]:
    runtime = BrowserRuntime(
        tmp_path,
        replay_recording_enabled=replay_recording_enabled,
    )
    runtime.artifacts.start_session()
    fake_page = MagicMock()
    fake_context = MagicMock()
    fake_context.tracing.start = AsyncMock()
    fake_context.pages = [fake_page]
    fake_browser = MagicMock()
    fake_browser.contexts = [fake_context]
    fake_chromium = MagicMock()
    fake_chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    runtime._playwright = MagicMock(chromium=fake_chromium)
    runtime._start_attached_screencast = AsyncMock()
    return runtime, fake_context
```

然后把现有 `test_managed_launch_uses_python_playwright_video_options` 拆成两个测试：

```python
@pytest.mark.asyncio
async def test_managed_launch_disables_replay_by_default(
    tmp_path: Path,
) -> None:
    runtime, fake_browser, fake_context = _managed_runtime_fixture(tmp_path)

    await runtime._launch_browser_session(headless=True)

    context_kwargs = fake_browser.new_context.await_args.kwargs
    assert "record_video_dir" not in context_kwargs
    assert "record_video_size" not in context_kwargs
    fake_context.tracing.start.assert_not_awaited()
    assert runtime.trace_active is False


@pytest.mark.asyncio
async def test_managed_launch_records_replay_when_enabled(
    tmp_path: Path,
) -> None:
    runtime, fake_browser, fake_context = _managed_runtime_fixture(
        tmp_path,
        replay_recording_enabled=True,
    )

    await runtime._launch_browser_session(headless=True)

    context_kwargs = fake_browser.new_context.await_args.kwargs
    assert context_kwargs["record_video_dir"]
    assert context_kwargs["record_video_size"] == {
        "width": 1280,
        "height": 800,
    }
    fake_context.tracing.start.assert_awaited_once_with(
        screenshots=True,
        snapshots=True,
        sources=True,
    )
    assert runtime.trace_active is True
```

增加完整 attached 测试：

```python
@pytest.mark.asyncio
async def test_attached_launch_disables_replay_by_default(
    tmp_path: Path,
) -> None:
    runtime, fake_context = _attached_runtime_fixture(tmp_path)

    await runtime._attach_browser_session(endpoint="http://127.0.0.1:9222")

    fake_context.tracing.start.assert_not_awaited()
    runtime._start_attached_screencast.assert_not_awaited()
    assert runtime.trace_active is False


@pytest.mark.asyncio
async def test_attached_launch_records_replay_when_enabled(
    tmp_path: Path,
) -> None:
    runtime, fake_context = _attached_runtime_fixture(
        tmp_path,
        replay_recording_enabled=True,
    )

    await runtime._attach_browser_session(endpoint="http://127.0.0.1:9222")

    fake_context.tracing.start.assert_awaited_once_with(
        screenshots=True,
        snapshots=True,
        sources=True,
    )
    runtime._start_attached_screencast.assert_awaited_once()
    assert runtime.trace_active is True
```

这些辅助函数只组装现有 MagicMock/AsyncMock，不在生产代码增加测试专用接口。

- [ ] **Step 5: 运行 runtime 测试并确认旧行为导致默认关闭用例失败**

```bash
.venv/bin/python -m pytest tests/unit/test_browser_runtime.py -k "replay or managed_launch or attached" -vv
```

Expected: 默认关闭测试 FAIL，因为当前仍配置视频并启动 tracing/screencast；显式开启测试描述现有兼容行为。

- [ ] **Step 6: 在两种启动模式中按策略启动 recorder**

managed context 先只构造 viewport/user-agent：

```python
context_options: dict[str, Any] = {
    "viewport": {"width": 1280, "height": 800},
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
}
if self.replay_recording_enabled:
    context_options.update({
        "record_video_dir": str(self.artifacts.get_video_dir()),
        "record_video_size": {"width": 1280, "height": 800},
    })
```

创建 context 后：

```python
if self.replay_recording_enabled:
    await self.context.tracing.start(
        screenshots=True,
        snapshots=True,
        sources=True,
    )
    self.trace_active = True
else:
    self.trace_active = False
    self.artifacts.append_event(
        "session_replay_recording_disabled",
        {"videos": False, "traces": False},
    )
```

attached 模式使用以下完整分支替换无条件 tracing，并用最后三行替换无条件
`_start_attached_screencast()`：

```python
if self.replay_recording_enabled:
    try:
        await self.context.tracing.start(
            screenshots=True,
            snapshots=True,
            sources=True,
        )
        self.trace_active = True
    except Exception as exc:
        self.trace_active = False
        self.artifacts.append_event(
            "session_trace_unavailable",
            {
                "sessionSource": self.session_source,
                "browserMode": self.browser_mode,
                "error": str(exc),
            },
        )
else:
    self.trace_active = False
    self.artifacts.append_event(
        "session_replay_recording_disabled",
        {"videos": False, "traces": False},
    )

if self.replay_recording_enabled:
    await self._start_attached_screencast()
```

- [ ] **Step 7: 写入 Engine 与隔离 runtime 传播的失败测试**

在 `tests/unit/test_task_runner.py` 增加：

```python
def test_isolated_runtime_inherits_replay_recording_policy(
    tmp_path: Path,
) -> None:
    shared = BrowserRuntime(
        tmp_path,
        replay_recording_enabled=True,
    )
    runner = TaskRunner(
        str(tmp_path),
        options={
            "runtime": shared,
            "planner": MagicMock(),
            "max_concurrent_runs": 2,
        },
    )

    isolated = runner._create_isolated_runtime("run-a")

    assert isolated.replay_recording_enabled is True
```

在 `tests/unit/test_engine.py` 增加使用临时 memory 路径的测试：

```python
@pytest.mark.asyncio
async def test_engine_passes_browser_replay_policy_to_runtime(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        workspace_root=str(tmp_path),
        browser={"replay_recording_enabled": True},
        memory={
            "session_db_path": str(tmp_path / "sessions.db"),
            "vector_db_path": str(tmp_path / "vectors"),
            "long_term_enabled": False,
        },
    )

    engine = AgentEngine(config)
    try:
        assert engine._browser_session.replay_recording_enabled is True
    finally:
        await engine.close()
```

- [ ] **Step 8: 传播策略并修正文案/调试能力**

`AgentEngine` 构造共享 runtime 时传入：

```python
self._browser_session = BrowserRuntime(
    self._runtime_data_dir / "browser",
    replay_recording_enabled=(
        config.browser.replay_recording_enabled
    ),
)
```

`TaskRunner.__init__()` 在创建默认 runtime 前保存策略，完整分支为：

```python
runtime = options.get("runtime")
if isinstance(runtime, BrowserRuntime):
    self._replay_recording_enabled = runtime.replay_recording_enabled
else:
    self._replay_recording_enabled = bool(
        options.get("replay_recording_enabled", False)
    )
if runtime is None:
    runtime = BrowserRuntime(
        base_dir,
        replay_recording_enabled=self._replay_recording_enabled,
    )
self.runtime = runtime
```

`_create_isolated_runtime()` 使用：

```python
return BrowserRuntime(
    run_dir,
    replay_recording_enabled=self._replay_recording_enabled,
)
```

这保证传入真实 `BrowserRuntime` 时从其属性继承；无 runtime 时使用 options 中的显式值，
默认仍为 `False`，且不会从无类型 MagicMock 的 truthiness 推断策略。

`get_debug_state()` 的 capability 精确改为：

```python
capabilities = (
    {
        "sessionReuse": True,
        "visibleBrowser": True,
        "videos": (
            self.replay_recording_enabled
            and self.attached_video_capability
        ),
        "traces": (
            self.replay_recording_enabled
            and self.trace_active
        ),
        "modeSwitching": False,
    }
    if self.session_source == "attached"
    else {
        "sessionReuse": True,
        "visibleBrowser": self.browser_mode == "headful",
        "videos": self.replay_recording_enabled,
        "traces": (
            self.replay_recording_enabled
            and self.trace_active
        ),
        "modeSwitching": True,
    }
)
```

`BrowserStopTool.description` 改为：

```python
return (
    "Stop the browser and finalize enabled browser artifacts. "
    "Use when the testing task is complete."
)
```

在 `tests/unit/test_browser_tools.py` 增加文案回归：

```python
def test_browser_stop_description_does_not_promise_trace() -> None:
    tool = BrowserStopTool(_make_runtime())

    assert "save debug trace" not in tool.description
    assert "enabled browser artifacts" in tool.description
```

- [ ] **Step 9: 运行相关单元测试**

```bash
.venv/bin/python -m pytest \
  tests/unit/test_config.py \
  tests/unit/test_browser_runtime.py \
  tests/unit/test_task_runner.py \
  tests/unit/test_engine.py \
  tests/unit/test_browser_tools.py -q
```

Expected: PASS；默认与显式开启两种策略、并发传播和工具文案均受覆盖。

- [ ] **Step 10: 用真实 Chromium 验证默认零回放与显式回放兼容**

```bash
.venv/bin/python - <<'PY'
import asyncio
import tempfile
from pathlib import Path

from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime


async def run_session(root: Path, enabled: bool) -> tuple[list[Path], list[Path]]:
    page = root / "page.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "<title>Replay Test</title><body><h1>Welcome</h1></body>",
        encoding="utf-8",
    )
    runtime = BrowserRuntime(
        root / "runtime",
        replay_recording_enabled=enabled,
    )
    try:
        await runtime.start({"source": "managed", "headless": True})
        await runtime.goto(page.as_uri())
        assert (await runtime.wait_for(text="Welcome"))["matched"] == "text"
        assert (await runtime.evaluate("document.title"))["result"] == "Replay Test"
    finally:
        await runtime.stop()
    return list(root.rglob("*.zip")), list(root.rglob("*.webm"))


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="naumi-replay-") as raw:
        root = Path(raw)
        disabled_zip, disabled_video = await run_session(
            root / "disabled",
            False,
        )
        enabled_zip, enabled_video = await run_session(
            root / "enabled",
            True,
        )
        assert disabled_zip == []
        assert disabled_video == []
        assert enabled_zip, "显式开启后应生成 trace ZIP"
        assert enabled_video, "显式开启后应生成 WebM 视频"


asyncio.run(main())
PY
```

Expected: exit 0；默认会话没有 `.zip`/`.webm`，显式开启会话两类文件均存在。

- [ ] **Step 11: 运行提交门禁、自我审视并提交**

```bash
ruff check src/
.venv/bin/python -c "from naumi_agent.config.settings import AppConfig; assert AppConfig().browser.replay_recording_enabled is False"
git diff --check
git add \
  src/naumi_agent/config/settings.py \
  src/naumi_agent/orchestrator/engine.py \
  src/naumi_agent/tools/browser/runtime/browser_runtime.py \
  src/naumi_agent/tools/browser/orchestrator/task_runner.py \
  src/naumi_agent/tools/browser/tools.py \
  tests/unit/test_config.py \
  tests/unit/test_browser_runtime.py \
  tests/unit/test_task_runner.py \
  tests/unit/test_engine.py \
  tests/unit/test_browser_tools.py
git commit -m "fix(browser): disable replay recording by default"
```

提交前逐项确认：默认路径是否仍有隐式 recorder、隔离 runtime 是否可能回退、显式开启
是否完整兼容、停止流程是否访问未启动对象、用户是否能从配置名理解磁盘影响。

---

### Task 4: 最终验证与不足声明

**Files:**
- Verify only; no production edits expected.

**Interfaces:**
- Consumes: Tasks 1-3 的三个独立提交。
- Produces: 可复现的质量证据、已知基线失败和最终自审结论。

- [ ] **Step 1: 运行全部静态与 import 检查**

```bash
ruff check src/
.venv/bin/python -c "import naumi_agent; from naumi_agent.orchestrator.engine import AgentEngine; from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime"
git diff --check
git status --short
```

Expected: ruff/import/diff 检查通过；工作区无未提交的本任务文件。

- [ ] **Step 2: 运行完整测试套件**

```bash
.venv/bin/python -m pytest tests/ -x
```

Expected: 若仓库基线已修复则全部 PASS；否则精确记录首个与本任务无关的既有失败，另行运行
Task 1-3 的相关测试集合并确认全部 PASS，不声称完整套件通过。

- [ ] **Step 3: 重跑完整真实浏览器闭环**

重跑 Task 3 Step 10，并额外通过 `BrowserWaitForTool` 与 `BrowserEvaluateTool` 调用工具层，
确认从用户/Agent 触发到结果输出、停止和 artifact 检查全链路一致。

- [ ] **Step 4: 多轮自我审视**

逐项回答并写入最终交付：

1. Playwright 调用是否匹配本机实际签名，还是只让 mock 通过？
2. `evaluate` 是否返回真实页面值，异常和 8 KiB 边界是否保留？
3. 所有 runtime 构造路径是否默认零 trace/视频/screencast？
4. 显式开启是否仍生成可用 ZIP/WebM？
5. 停止、并发、attached 模式和错误路径是否有遗漏？
6. 完整测试是否存在基线阻塞，是否已明确区分本任务结果？

- [ ] **Step 5: 不创建额外合并提交**

最终保持三个功能提交与两个文档提交的边界，不把无关基线修复、格式化或清理混入。
