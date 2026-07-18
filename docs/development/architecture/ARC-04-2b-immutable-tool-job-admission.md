# ARC-04.2b Immutable ToolJob Admission

## 1. 交付目标

在 Runtime 与未来 Tool daemon 之间建立不可旁路的 durable admission envelope。一个 Job 只有在以下四个
authority 同时同意时才能进入 `admitted`：

1. ARC-04.2a execution grant 对当前 request 仍有效；
2. Worker Registry 的 active Tool incarnation 与 grant 完全一致；
3. Worker heartbeat、accepting 状态、容量、能力、资源和隔离满足明确 requirements；
4. Harness Tool lease 仍 active，owner/epoch/expiry 未变化。

本切片不启动 OS daemon，也不执行命令；它完成 admission 和 dispatch 前复验边界，避免 ARC-04.3、HAR-08.4
或 EVO-03.6 各自发明旁路。

## 2. ImmutableToolJob schema v1

合同持久化以下不可变事实：

- session/run/call/tool/tool-family 与 caller idempotency key；
- canonical arguments SHA-256；原始 command、env、secret 和 cwd 不进入数据库；
- workspace SHA-256、execution grant id/digest、permission receipt reference；
- Worker id/instance/epoch/contract digest；
- Tool lease owner/epoch；
- `WorkerAdmissionRequirements` digest；
- admitted/expiry、request digest 和完整 job digest。

Job expiry 继承 execution grant，不可延长。SQLite 以 idempotency key 串行 reserve；相同 request 重放返回首次
job id，不同 request 冲突且不覆盖。SHA-256 提供内容完整性，不替代未来 ARC-02 authenticated transport。

## 3. Admission 与 dispatch 复验

`ToolJobAuthority.admit()` 先调用 `ExecutionGrantAuthority.validate()`，再调用
`WorkerRegistryStore.assess_admission()`，最后才持久化合同。grant missing/revoked/expired、参数变化、Worker
takeover、lease release、heartbeat unhealthy、capacity exhausted、capability/resource/isolation 不足均 fail closed。

`validate_for_dispatch()` 不信任 admission 时的旧快照，会重新读取 execution grant、Worker Registry、heartbeat
和 lease，并核对 request 与 requirements digest。任一 authority 改变都会返回机械 reason；daemon producer
必须只消费 `allowed=true` 的结果。

## 4. Store 与 Composition

- `runtime_data_dir/tool-jobs.db`，SQLite `user_version=1`，惰性创建；POSIX 目录/文件为 0700/0600；
- 未版本化非空库、未来 schema、wrong-type path、contract/index 篡改均拒绝；
- Runtime Composition Root 注入唯一 `ToolJobStore` 和 `ToolJobAuthority`；
- Store Catalog 登记 `runtime.tool_jobs`，restricted、audit-long-term；Doctor 只读检查不物化数据库。

## 5. 验收证据

- 真实 Worker Registry、heartbeat、Harness lease、permission receipt、execution grant 完成
  admit→关闭→reopen→dispatch validate；
- 8 个并发相同 admission 只产生一行和一个 job id；
- 原始 secret 不出现在 ToolJob database bytes；
- 参数/requirements 变化、grant revoke、Worker takeover、容量耗尽、资源不足均 fail closed；
- tamper、future schema、wrong path type、non-Tool requirements 均拒绝；
- Composition、RuntimePaths、Store Catalog 和 Doctor 使用同一物理路径事实。

## 6. 诚实边界与下一步

- durable contract 只保存执行 envelope，不保存 raw payload；未来 authenticated daemon transport 必须携带原始
  arguments，并在 dispatch 时用 digest 复核。Runtime 在 transport 前崩溃时不能从本 Store 还原 secret payload；
- 尚无 queued/running/completed/failed/unknown 状态机、completion receipt、cancel 或 crash recovery；
- 尚无真实 Shell worker、process tree、artifact writer 或 OS sandbox，因此 HAR-08.4 仍为 planned；
- 下一最小跨文档切片应实现 ARC-04.2c ToolJob lifecycle receipt：先冻结 dispatch/running/terminal/unknown
  单调状态和副作用分类，再让 ARC-04.3a non-PTY Shell worker 消费，避免先执行后补幂等语义。
