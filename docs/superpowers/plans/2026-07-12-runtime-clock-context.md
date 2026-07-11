# Runtime Clock Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在每轮临时 Harness 快照中注入可信的本地时间和明确时区，使模型无需工具即可回答当前时间问题。

**Architecture:** 扩展现有 `HarnessContextAssembler`，通过构造函数接收同步时钟函数，默认读取 `datetime.now().astimezone()`。组装时生成独立的“当前环境”段；现有同步与流式 ReAct 循环已经逐轮替换 Harness 快照，因此无需修改引擎或前端。

**Tech Stack:** Python 3.14、标准库 `datetime`、pytest、Ruff

## Global Constraints

- 时间只进入临时 Harness 快照，不写入持久化 system prompt。
- 默认时钟只读取本机系统时间，不访问网络、不触发权限确认。
- 输出必须包含 ISO 8601 秒级时间戳、时区名称和 `UTC+HH:MM` 偏移。
- 无时区 `datetime` 必须转换为本机带时区时间。
- 不新增 Tool、斜杠命令或前端专用逻辑。

---

### Task 1: Inject a trustworthy runtime clock into Harness context

**Files:**
- Modify: `src/naumi_agent/orchestrator/context_assembly.py`
- Test: `tests/unit/test_context_assembly.py`

**Interfaces:**
- Consumes: `HarnessContextAssembler(clock: Callable[[], datetime] | None = None)`
- Produces: `HarnessContextAssembler._environment_section() -> str`
- Preserves: `HarnessContextAssembler.assemble(data: HarnessContextInput) -> str`

- [ ] **Step 1: Write failing fixed-clock and refresh tests**

Add imports and tests that inject deterministic clocks through the real engine snapshot path:

```python
from datetime import datetime, timedelta, timezone

from naumi_agent.orchestrator.context_assembly import HarnessContextAssembler


@pytest.mark.asyncio
async def test_harness_context_snapshot_includes_trusted_local_time(
    engine: AgentEngine,
) -> None:
    fixed = datetime(
        2026, 7, 12, 3, 22, 36,
        tzinfo=timezone(timedelta(hours=8), name="Asia/Shanghai"),
    )
    engine._harness_context = HarnessContextAssembler(clock=lambda: fixed)

    await engine._inject_harness_context_snapshot()

    content = engine._messages[-1]["content"]
    assert "### 当前环境" in content
    assert "当前本地时间：2026-07-12T03:22:36+08:00" in content
    assert "时区：Asia/Shanghai (UTC+08:00)" in content
    assert "可直接回答，无需调用工具或公网 API" in content


@pytest.mark.asyncio
async def test_harness_context_clock_refreshes_each_snapshot(
    engine: AgentEngine,
) -> None:
    times = iter((
        datetime(2026, 7, 12, 3, 22, tzinfo=timezone.utc),
        datetime(2026, 7, 12, 3, 23, tzinfo=timezone.utc),
    ))
    engine._harness_context = HarnessContextAssembler(clock=lambda: next(times))

    await engine._inject_harness_context_snapshot()
    first = engine._messages[-1]["content"]
    await engine._inject_harness_context_snapshot()
    second = engine._messages[-1]["content"]

    assert "2026-07-12T03:22:00+00:00" in first
    assert "2026-07-12T03:23:00+00:00" in second
    assert "2026-07-12T03:22:00+00:00" not in second
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest \
  tests/unit/test_context_assembly.py::test_harness_context_snapshot_includes_trusted_local_time \
  tests/unit/test_context_assembly.py::test_harness_context_clock_refreshes_each_snapshot -q
```

Expected: both tests fail because `HarnessContextAssembler` does not accept `clock`.

- [ ] **Step 3: Implement the clock and environment section**

In `context_assembly.py`, import `Callable`, `datetime`, and `timedelta`, then add:

```python
Clock = Callable[[], datetime]


class HarnessContextAssembler:
    """Build a compact, current-state snapshot for the model."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or _local_now

    async def assemble(self, data: HarnessContextInput) -> str:
        sections = [
            "## Harness 状态快照",
            "这是每轮自动生成的运行环境快照，用来帮助你选择下一步工具和恢复长期任务。",
            self._environment_section(),
            # Existing sections remain in their current order.
        ]

    def _environment_section(self) -> str:
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.astimezone()
        offset = _format_utc_offset(current.utcoffset())
        zone_name = current.tzname() or f"UTC{offset}"
        return (
            "### 当前环境\n"
            f"- 当前本地时间：{current.isoformat(timespec='seconds')}\n"
            f"- 时区：{zone_name} (UTC{offset})\n"
            "- 时间问题：以上是本轮可信时间；可直接回答，无需调用工具或公网 API。"
        )


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _format_utc_offset(offset: timedelta | None) -> str:
    total_minutes = int((offset or timedelta()).total_seconds() / 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command again.

Expected: `2 passed`.

- [ ] **Step 5: Add and verify the naive-clock boundary test**

```python
@pytest.mark.asyncio
async def test_harness_context_normalizes_naive_clock_to_local_timezone(
    engine: AgentEngine,
) -> None:
    engine._harness_context = HarnessContextAssembler(
        clock=lambda: datetime(2026, 7, 12, 3, 22, 36),
    )

    await engine._inject_harness_context_snapshot()

    content = engine._messages[-1]["content"]
    assert "当前本地时间：2026-07-12T03:22:36" in content
    assert "- 时区：" in content
    assert "(UTC+" in content or "(UTC-" in content
```

Run:

```bash
uv run pytest tests/unit/test_context_assembly.py -q
```

Expected: all context assembly tests pass.

- [ ] **Step 6: Run quality and engine regression checks**

Run:

```bash
uv run ruff check \
  src/naumi_agent/orchestrator/context_assembly.py \
  tests/unit/test_context_assembly.py
uv run pytest \
  tests/unit/test_context_assembly.py \
  tests/unit/test_engine.py -q
```

Expected: Ruff exits 0 and all selected tests pass.

- [ ] **Step 7: Run a real-clock smoke check**

Run the real `HarnessContextAssembler` through an `AgentEngine` instance and print only the `### 当前环境` section. Confirm the timestamp is within one minute of `date`, contains the local offset, and no network or permission event occurs.

- [ ] **Step 8: Self-review and commit**

Review the diff against `docs/superpowers/specs/2026-07-12-runtime-clock-context-design.md`, then run `git diff --check`.

```bash
git add \
  docs/superpowers/plans/2026-07-12-runtime-clock-context.md \
  src/naumi_agent/orchestrator/context_assembly.py \
  tests/unit/test_context_assembly.py
git commit -m "fix: inject runtime clock into agent context"
```
