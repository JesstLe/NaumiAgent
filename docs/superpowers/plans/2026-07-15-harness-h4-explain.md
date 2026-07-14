# Harness H4.4 Explain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为持久化 Harness 运行提供确定性失败分类和双通道中文解释。

**Architecture:** 新增纯 `HarnessExplainer` 消费 `HarnessStoredRun` 并产生冻结的结构化解释；`HarnessService` 只负责当前工作区查询与 Store 错误降级；只读 Agent Tool 与 `/harness explain` 共享 Service 和 renderer。分类结果不写回数据库，不调用模型或工具。

**Tech Stack:** Python 3.12+、dataclasses、StrEnum、HarnessStore/SQLite、Rich Markdown、pytest-asyncio、ruff。

## Global Constraints

- 只实现 H4.4 Explain，不实现 Replay、UI 卡片、Eval 或 LLM Judge。
- Tool 与 slash 命令必须调用同一个 `HarnessService.explain_run()`。
- 只使用 Store 的规范化字段；不读取原始 stdout、参数、权限原因或 reasoning。
- 显式 run id 必须通过当前工作区隔离检查。
- 只运行 Explain、Store、Surfaces、Tools 定向测试，不运行全量测试。

---

### Task 1: Deterministic run explanation

**Files:**
- Create: `src/naumi_agent/harness/explain.py`
- Create: `tests/unit/test_harness_explain.py`
- Modify: `src/naumi_agent/harness/service.py`
- Modify: `src/naumi_agent/harness/tools.py`
- Modify: `src/naumi_agent/main.py`
- Modify: `src/naumi_agent/cli/completer.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `tests/unit/test_harness_surfaces.py`
- Modify: `tests/unit/test_harness_knowledge_tool.py`
- Modify: `docs/superpowers/specs/2026-07-14-harness-engineering-design.md`

**Interfaces:**
- Produces: `HarnessExplainer.explain(run: HarnessStoredRun) -> HarnessRunExplanation`。
- Produces: `HarnessService.explain_run(run_id: str | None = None) -> HarnessExplainLookup`。
- Produces: `render_harness_explanation(result: HarnessExplainLookup) -> str`。
- Produces: read-only `harness_explain` Tool and `/harness explain [run-id|latest]`。

- [x] **Step 1: Write classifier RED tests**

  构造真实 `HarnessStoredRun` fixture，断言 verified/running 不误分类；failed check、
  permission denial、skipped tool、invalid tool、missing evidence warning、Store warning 和
  unclassified blocked 分别映射到规范分类，并且 finding 含中文下一步。

- [x] **Step 2: Verify classifier tests are RED**

  Run: `.venv/bin/python -m pytest tests/unit/test_harness_explain.py -q`

  Expected: collection fails because `naumi_agent.harness.explain` does not exist.

- [x] **Step 3: Implement pure HarnessExplainer**

  使用 `HarnessFailureClass(StrEnum)`、冻结 dataclass 和无副作用规则函数；按设计优先级
  收集 finding、同类去重并保持稳定顺序。Renderer 只展示安全字段和 evidence digest
  前 12 位。

- [x] **Step 4: Write Service lookup RED tests**

  使用两个真实临时 workspace 和同一 Store，验证 latest、显式 run id、跨工作区按
  not_found、无 Store 为 unavailable、损坏库为 safe unavailable。

- [x] **Step 5: Integrate Service lookup**

  `run_id=None|latest` 调用 `list_runs(workspace_root, limit=1)`；显式 id 调用 `get_run()`
  后用 canonical workspace 比较；捕获 `HarnessStoreError` 返回安全中文消息。

- [x] **Step 6: Write Tool and slash RED tests**

  断言 `create_harness_tools()` 注册只读并发安全 `harness_explain`；通过真实 Store 完成
  一次 failed check 运行后，`execute_slash_command(engine, "/harness explain latest")`
  输出 run id、`verification_failure`、原因和下一步；非法多参数显示用法。

- [x] **Step 7: Wire shared surfaces**

  Tool 参数只允许可选 `run_id` 字符串；main usage 增加 explain；CLI completer 和新 UI
  slash registry 描述增加“解释”；slash 分支只调用 Service 与 renderer。

- [x] **Step 8: Targeted verification and real smoke**

  Run: `.venv/bin/python -m pytest tests/unit/test_harness_explain.py tests/unit/test_harness_store.py tests/unit/test_harness_surfaces.py tests/unit/test_harness_knowledge_tool.py -q`

  Run: `.venv/bin/ruff check src/naumi_agent/harness/explain.py src/naumi_agent/harness/service.py src/naumi_agent/harness/tools.py src/naumi_agent/main.py src/naumi_agent/cli/completer.py src/naumi_agent/ui/bridge.py tests/unit/test_harness_explain.py tests/unit/test_harness_surfaces.py tests/unit/test_harness_knowledge_tool.py`

  Run: `.venv/bin/python -m compileall -q src/naumi_agent/harness/explain.py src/naumi_agent/harness/service.py src/naumi_agent/harness/tools.py`

  Expected: selected tests, lint, compile and `git diff --check` pass. Real smoke must restore a completed run from a new `HarnessStore` and render the same classification through Tool and slash.

- [x] **Step 9: Self-review, commit, and push**

  确认分类完全机械、跨工作区不可探测、无 raw event 泄漏、running 不误报失败、每条失败
  含下一步；随后提交 `feat: explain harness run failures` 并推送 `main`。
