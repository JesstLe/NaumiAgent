# Harness H4.3 Evidence Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 Harness run 中真实工具执行的规范化事实持久化为可查询 Evidence，并为后续 Explain/Replay 提供稳定索引。

**Architecture:** 新增 Engine-neutral `EvidenceCollector`，按 `(run_id, call_id)` 关联 `tool_start` 与 `tool_end`，只持久化脱敏参数 digest、脱敏结果 digest、状态、耗时和 ChatRun URI，不复制 stdout、reasoning 或认证信息。`HarnessService` 管理 Collector 生命周期与 Store 故障警告；`AgentEngine` 只转发规范化事件，即使 UI callback 缺失也会收集证据。

**Tech Stack:** Python 3.12、asyncio、SHA-256、HarnessStore、Pydantic Evidence refs、pytest-asyncio、ruff。

## Global Constraints

- 本切片只实现 H4.3 EvidenceCollector，不实现 Explain、Replay、失败分类器或 UI 卡片。
- `_execute_tool` 成功或失败后只发送规范化事件；Harness 不包裹或绕过 Tool execute。
- Evidence 不保存 raw stdout、完整参数、secret、认证 header 或模型 reasoning。
- 并发工具调用必须按 call id 隔离；重复 end 必须幂等；缺失 start 必须保留可解释标记。
- HarnessStore 写入失败不得丢失工具结果或主任务结果，只增加 `infrastructure_error` warning。
- 只运行 Evidence、Store、Run Gate 与 Engine Gate 定向测试，不运行全量测试。

---

### Task 1: Collect normalized tool execution evidence

**Files:**
- Create: `src/naumi_agent/harness/evidence.py`
- Create: `tests/unit/test_harness_evidence.py`
- Modify: `src/naumi_agent/harness/service.py`
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `tests/unit/test_harness_runtime_persistence.py`
- Modify: `docs/superpowers/specs/2026-07-14-harness-engineering-design.md`

**Interfaces:**
- Produces: `EvidenceCollector.observe(run_id: str, event: str, data: Mapping[str, Any]) -> HarnessEvidenceRef | None`。
- Produces: `EvidenceCollector.list_refs(run_id) -> tuple[HarnessEvidenceRef, ...]` 和 `forget_run(run_id)`。
- Produces: `HarnessService.observe_tool_event(run_id, event, data)`，负责 Store 错误降级。
- Consumes: `HarnessStore.record_evidence()`、当前 `HarnessRunState` 与 Engine `tool_start/tool_end` 数据。

- [x] **Step 1: Write collector RED tests**

  使用真实临时 `HarnessStore` 和已 start 的 run，验证 start/end 产生一条 `tool_execution` Evidence；数据库只含 digest、状态、耗时和 URI，不含参数值、stdout 或 API key。验证 `tool_end` 重复到达只保留一条记录。

- [x] **Step 2: Write concurrency and missing-start RED tests**

  并发交错 20 个 call id，断言每个结果与自己的参数 digest 配对；单独 end 仍生成 `start_missing: true` 的证据；未知事件不写库。

- [x] **Step 3: Verify collector tests are RED**

  Run: `.venv/bin/python -m pytest tests/unit/test_harness_evidence.py -q`

  Expected: collection fails because `naumi_agent.harness.evidence` does not exist.

- [x] **Step 4: Implement EvidenceCollector**

  使用有界 `OrderedDict`、`asyncio.Lock` 和确定性 evidence id；敏感 key 直接替换，字符串经过 `OutputGuardrail.redact`，canonical JSON 后再计算 SHA-256。`tool_end` 保存 `chat-run://<run>/tool/<call>` URI、event digest 与 bounded human summary。

- [x] **Step 5: Integrate through HarnessService**

  仅对当前进程成功持久化且未 finalized 的 run 接受事件；Collector 抛出 `HarnessStoreError` 时调用现有 warning 路径。Gate 合并显式 Evidence 与 Collector refs，run final 后清理 Collector 内存状态。

- [x] **Step 6: Write and verify Engine RED test**

  用真实只读测试 Tool 调用 `AgentEngine._execute_tool_calls(..., on_event=None)`，断言 Harness Evidence 仍写入 Store；在接入前测试必须因 evidence 为空失败。

- [x] **Step 7: Forward normalized Engine events**

  `tool_start` 与 `tool_end` 各构造一次公共 payload，同时交给 HarnessService 和现有 UI callback；Collector 故障不得改变 ToolResult。payload 增加 `read_only`、`destructive` 与 batch metadata，但不改变既有字段。

- [x] **Step 8: Run targeted verification and real smoke**

  Run: `.venv/bin/python -m pytest tests/unit/test_harness_evidence.py tests/unit/test_harness_runtime_persistence.py tests/unit/test_harness_store.py tests/unit/test_harness_run_gate.py tests/unit/test_harness_engine_gate.py -q`

  Run: `.venv/bin/ruff check src/naumi_agent/harness/evidence.py src/naumi_agent/harness/service.py src/naumi_agent/orchestrator/engine.py tests/unit/test_harness_evidence.py tests/unit/test_harness_runtime_persistence.py`

  Expected: all selected tests and lint pass. Real smoke must execute a real Tool through Engine with no UI callback and recover its Evidence from a new Store instance.

- [x] **Step 9: Self-review, commit, and push**

  确认无 raw content/args 写盘、并发配对正确、内存有界、Store failure 只告警；随后提交 `feat: collect harness tool evidence` 并推送 `main`。
