# ARC-04.1c Durable Worker Health Report Authority

## 状态

已实现。

## 目标

把原本只能由调用方临时传入的 `WorkerHealthReport` 纳入 Runtime-owned Worker Registry，使后续调度从 durable
authority 读取 exact active incarnation 的 heartbeat、active jobs 和 accepting-jobs，而不是相信命令参数或旧内存。

## Registry v5

`worker_health_reports` 以 `worker_id + epoch + heartbeat sequence` 为主键，冻结：

- exact instance、epoch、Worker Contract SHA；
- heartbeat workspace/kind/subject/instance/epoch/sequence/phase/time/timeout/detail code；
- active jobs、accepting-jobs 与完整 report SHA；
- observed/recorded timestamps。

写入在 `BEGIN IMMEDIATE` 中重新读取 active registration。相同 sequence + 相同 artifact 幂等；同 sequence 不同内容、
sequence 回退、observed time 回退、stale incarnation、future observation、非 canonical workspace/time 和 digest/index
篡改全部失败关闭。并发首次写入收敛到同一 durable report。

v4 到 v5 只新增表和索引；v1-v4 均按既有显式迁移链进入 v5。未来 schema、错误文件类型和未版本化非空数据库继续
拒绝自动猜测。

## 消费边界

- `record_health_report()` 是唯一写入入口；
- `get_latest_health_report()` 只返回当前 active incarnation 的最新 report，Worker takeover 后旧 epoch 自动 fencing；
- `assess_latest_admission()` 组合 durable report 与 current active contract，继续复用既有
  `assess_worker_admission()` 的 heartbeat freshness、accepting、capacity、capability/resource/isolation 检查；
- 无 active registration 返回 `registration_missing`，有 registration 无 report 返回 `health_not_ready`。

SHA-256 证明 durable 内容完整性，不证明远端来源身份。跨主机 producer 仍必须经过后续 authenticated transport/claim
边界；本 authority 不接受网络请求、不预留 capacity、不派发或执行 Job。

## 验收证据

- 真实 Worker Contract + typed heartbeat 可并发持久化、关闭并重开；
- durable latest report 可直接驱动 admission，`accepting_jobs=false` 机械阻断；
- higher epoch takeover 后旧 health 不再可见且不能重写；
- index tamper fail closed；v4 数据库原子迁移到 v5；
- Worker Registry 小模块测试、Ruff、py_compile、文档治理和 diff check 通过；未运行全量测试。

## 后续

HAR-09.6c2a3b 已把 current Target Baseline Resolution、Placement、durable latest health 与 atomic capacity
reservation 组合成 post-rollback queued Dispatch。下一权威边界是 authenticated Worker claim/lease，当前仍没有
执行或结果权威。
