# Harness H4.2 Runtime Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让受信任 Harness 运行的 Profile、开始状态、检查结果、最终 Contract 与 Receipt 自动写入 `HarnessStore`，并在存储失败时保留主任务结果且给出明确警告。

**Architecture:** `HarnessService` 继续作为唯一生命周期所有者，可选接收 `HarnessStore`；`AgentEngine` 只注入用户级真实 Store，不直接双写。Service 对当前进程内成功创建的持久化 run 做有界跟踪，按 begin → check → finish 顺序写入；任何 Store 故障都转成非阻断的 `infrastructure_error` 回执警告，不把 SQLite/OSError 暴露给用户。

**Tech Stack:** Python 3.12、aiosqlite、Pydantic completion contracts、pytest-asyncio、ruff。

## Global Constraints

- 本切片只做 H4.2 生命周期持久化，不实现 EvidenceCollector、Explain、Replay、Session 删除联动或 UI 卡片。
- HarnessStore 写入失败不得丢失主任务结果，但必须生成 `infrastructure_error` 警告。
- Profile 未信任时不得创建 Harness run 或写运行数据。
- 只运行 Harness Store、Completion、Run Gate 和 Engine Gate 定向测试，不运行全量测试。
- 所有用户可见错误使用中文；代码注释和 commit message 使用英文。

---

### Task 1: Persist the live Harness lifecycle

**Files:**
- Create: `tests/unit/test_harness_runtime_persistence.py`
- Modify: `src/naumi_agent/harness/service.py`
- Modify: `src/naumi_agent/harness/completion.py`
- Modify: `src/naumi_agent/harness/store.py`
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `tests/unit/test_harness_completion.py`
- Modify: `tests/unit/test_harness_engine_gate.py`
- Modify: `docs/superpowers/specs/2026-07-14-harness-engineering-design.md`

**Interfaces:**
- Consumes: `HarnessStore.record_profile()`, `start_run()`, `record_check()`, `finish_run()` 和现有 `HarnessService` 生命周期。
- Produces: `HarnessService(..., store: HarnessStore | None = None)`、`HarnessService.store`、`CompletionGateInput.informational_warnings`。
- Extends: `HarnessStore.finish_run(..., contract: HarnessCompletionContract | None = None)`，用于保存 Gate 最终选择的 task kind 与 required checks。

- [x] **Step 1: Write the automatic lifecycle failing test**

  使用真实临时 Git 工作区、真实 Profile、真实 `HarnessTrustStore` 和真实 `HarnessStore`：调用 `begin_completion_run()`、修改 Python 文件、执行真实 Profile check、完成 Gate，再从新 Store 实例读取。断言 run 为 `completed_verified`、最终 Contract 为 `change` 且包含 `unit`、Check 已落库、原始命令输出未进入数据库。

- [x] **Step 2: Verify the lifecycle test is RED**

  Run: `.venv/bin/python -m pytest tests/unit/test_harness_runtime_persistence.py::test_service_persists_live_run_lifecycle -q`

  Expected: FAIL because `HarnessService` does not accept `store` and runtime operations are not persisted.

- [x] **Step 3: Write the real storage-failure test**

  将 Store parent 指向一个真实普通文件，使 SQLite 状态目录无法创建；断言 `begin_completion_run()` 和最终 completion 仍返回，receipt 保持原完成状态并包含去重后的 `infrastructure_error` 中文警告，而非泄漏 `FileExistsError` 或 SQL。

- [x] **Step 4: Write CompletionGate informational warning test**

  构造无其他 issue 的 `CompletionGateInput(informational_warnings=(... ,))`，断言状态仍是 `completed_verified`，警告只进入 receipt，不触发 correction 或 blocked。

- [x] **Step 5: Implement Service-owned persistence**

  `begin_completion_run()` 先构建原有 state，再保存 Profile 与 run；`run_check()` 先保留内存结果，再保存脱敏 Check metadata；`evaluate_completion_run()` 将有界 Store warning 注入 Gate，并在 final 时用最终 Contract 写 receipt。仅对当前进程成功 start 的 run 写 check/finish，避免手动孤立 check 产生外键噪声。

- [x] **Step 6: Inject the default Store in AgentEngine**

  初始化使用 `HarnessStore(resolve_harness_db_path())`；增加定向断言证明路径位于用户状态目录而不是工作区或 session 数据库。

- [x] **Step 7: Harden Store initialization failures**

  将 parent `mkdir/chmod` 的 OSError 统一包装为 `HarnessStoreError`，保证 Service 只处理稳定错误契约；`finish_run(contract=...)` 原子更新最终脱敏 Contract 与 receipt，且拒绝不可变身份或 criteria 被替换。

- [x] **Step 8: Run targeted verification**

  Run: `.venv/bin/python -m pytest tests/unit/test_harness_runtime_persistence.py tests/unit/test_harness_store.py tests/unit/test_harness_completion.py tests/unit/test_harness_run_gate.py tests/unit/test_harness_engine_gate.py -q`

  Expected: all selected tests pass.

  Run: `.venv/bin/ruff check src/naumi_agent/harness/service.py src/naumi_agent/harness/completion.py src/naumi_agent/harness/store.py src/naumi_agent/orchestrator/engine.py tests/unit/test_harness_runtime_persistence.py tests/unit/test_harness_completion.py tests/unit/test_harness_engine_gate.py`

  Expected: no lint errors.

- [x] **Step 9: Run a real persistence and degradation smoke test**

  不使用 mock：一条真实运行跨 Store 实例恢复；一条真实不可写状态路径仍产生 completion receipt，且警告不改变完成状态。

- [x] **Step 10: Self-review, commit, and push**

  确认内存跟踪有界、Store 故障不阻断主任务、未信任 Profile 不落 run、无原始输出/secret 写盘；随后提交 `feat: persist live harness runs` 并推送 `main`。
