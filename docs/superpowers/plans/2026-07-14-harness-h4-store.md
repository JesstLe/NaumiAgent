# Harness H4.1 Persistent Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Harness Profile、运行、验收条件、检查结果和证据提供可恢复、可查询、可幂等迁移的 SQLite 持久化层。

**Architecture:** 使用独立的用户状态库 `harness.db`，避免与聊天会话生命周期耦合。`HarnessStore` 只保存结构化元数据、摘要和外部 artifact 引用，不复制大文件、环境变量、认证头或模型 reasoning；所有运行记录都绑定规范化工作区，并通过外键事务保证运行、条件、检查和证据的一致性。

**Tech Stack:** Python 3.12、aiosqlite、dataclasses、Pydantic Harness contracts、pytest-asyncio、ruff。

## Global Constraints

- 本切片只实现 H4.1 Store，不实现 Explain、Replay、失败分类、UI 或 H5/H6 表。
- 只运行 Harness Store 及直接相关的小模块测试，不运行全量测试。
- 所有用户可见错误使用中文；代码注释和 commit message 使用英文。
- SQLite 迁移必须幂等，写操作必须事务化，Unix 状态目录和数据库权限分别收敛到 `0700` 与 `0600`。
- 原始大文件只保存 URI/path、SHA-256 和摘要，不写入 SQLite。

---

### Task 1: Implement the durable Harness store

**Files:**
- Create: `src/naumi_agent/harness/store.py`
- Create: `tests/unit/test_harness_store.py`
- Modify: `docs/superpowers/specs/2026-07-14-harness-engineering-design.md`

**Interfaces:**
- Consumes: `HarnessCompletionContract`, `HarnessCompletionReceipt`, `HarnessCheckResult`, `HarnessEvidenceRef`。
- Produces: `resolve_harness_db_path()`, `HarnessStore.record_profile()`, `start_run()`, `record_check()`, `record_evidence()`, `finish_run()`, `get_run()`, `list_runs()`, `delete_session_records()`。
- Produces records: `HarnessStoredProfile`, `HarnessStoredRun`, `HarnessStoredCriterion`, `HarnessStoredCheck`, `HarnessStoredEvidence`。

- [x] **Step 1: Write failing path and migration tests**

  在 `tests/unit/test_harness_store.py` 中验证默认路径位于 `NAUMI_STATE_HOME/harness.db`、首次写入创建五张 H4 表、`PRAGMA user_version = 1`，并验证重复初始化不会破坏数据。

- [x] **Step 2: Run the tests and verify RED**

  Run: `.venv/bin/python -m pytest tests/unit/test_harness_store.py -q`

  Expected: collection fails because `naumi_agent.harness.store` does not exist.

- [x] **Step 3: Add lifecycle, idempotency, and isolation tests**

  用真实临时 SQLite 数据库写入 Profile、Run、Criterion、Check 和 Evidence，完成 run 后从新 `HarnessStore` 实例读取；验证同一 payload 可重试、冲突 payload 被拒绝、工作区查询隔离、session reconciliation 级联删除子记录、并发 evidence 写入不丢失。

- [x] **Step 4: Implement the store**

  `store.py` 使用显式 schema v1 migration、外键、WAL、busy timeout 和写锁；运行完成事务同时更新 `harness_runs.receipt_json` 与 criterion 状态，读取时从规范 JSON 重建不可变 records。

- [x] **Step 5: Run targeted verification**

  Run: `.venv/bin/python -m pytest tests/unit/test_harness_store.py tests/unit/test_harness_completion.py tests/unit/test_harness_checks.py -q`

  Expected: all selected tests pass.

  Run: `.venv/bin/ruff check src/naumi_agent/harness/store.py tests/unit/test_harness_store.py`

  Expected: no lint errors.

- [x] **Step 6: Run a real persistence smoke test**

  在临时目录中用两个独立 `HarnessStore` 实例完成一次真实写入和读取，检查 Unix 权限、run 状态、criterion 状态、check/evidence 数量及 session 删除后的级联结果。

- [x] **Step 7: Self-review, commit, and push**

  检查没有 secret/raw output 字段、没有 H5/H6 空表、所有冲突有确定性错误；随后提交 `feat: add durable harness store` 并推送 `main`。
