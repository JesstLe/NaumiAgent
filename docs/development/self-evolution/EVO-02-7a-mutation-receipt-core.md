# EVO-02.7a 不可变 Mutation Receipt Core v1

## 目标

把已经 committed 的隔离 Patch 写入收敛成一份可交给 HAR-08/EVO-03 的不可变变异产物。Receipt 必须
证明本次变异来自同一 Contract、Lease、Snapshot、Mutation Plan、Static Guard、Writer 与 Postflight
权威链，不能由调用方手填“成功”字段，也不能把尚未提交或已经漂移的 worktree 宣称为可验证候选。

本切片只表示 `mutation_completed=true` 与 `validation_ready=true`。它固定
`validation_status=pending`、`promotion_ready=false`、`execution_ready=false`；通过 Mutation Receipt
不代表测试通过、指标改善或允许推广。

## 事实来源

`EvolutionMutationReceiptService.finalize()` 不接受外部 attempt、diff 或 tool evidence 参数，而是读取：

- 当前 Contract、active Lease、Source Snapshot 与 Mutation Plan；
- 同 Plan 且 `preflight_passed=true` 的 Static Guard Receipt；
- `EvolutionPatchJournalStore` 或 `EvolutionPatchSetStore` 中唯一 committed 事务；
- Store 中经过摘要复核的 Writer Receipt v2；
- 从精确 Git baseline 与当前 worktree 字节重新计算的 Postflight Diff/API Receipt。

同一 Lease 同时出现两个 committed Patch 来源时返回 `mutation_write_ambiguous`；没有 committed 写入时
返回 `mutation_write_not_committed`。历史 Writer Receipt v1 缺少完整 Postflight，仍可用于恢复，但不能
升级成自进化验证候选。

## Receipt 内容

`EvolutionMutationReceipt` 使用 canonical JSON SHA-256 生成 `evmr_` identity，包含：

- Contract manifest、Lease、Snapshot、Plan、Candidate revision 的完整 provenance；
- finding code、approved scope、Plan hypothesis 形成的 rationale 及其摘要；
- 单/多文件 writer kind、journal/transaction ID、Writer Receipt ID/digest；
- attempt/max attempts；
- 每个文件的 before/after digest、unified diff digest、增删行数和 API change；
- RED/GREEN 后续必须使用的相同 metric names；
- Static Guard、Patch Writer、Postflight Guard 三阶段有序治理工具证据；
- pending validation 与不可推广状态。

Receipt 不保存源码、diff 正文、绝对 worktree 路径或 backup。rationale 在持久化前复用 Static Guard secret
detector；疑似机密返回 `mutation_rationale_secret`，不写入 SQLite。

## 持久化与并发

`EvolutionMutationReceiptStore` 在 runtime SQLite 中创建 `evolution_mutation_receipts`：

- `mutation_receipt_id` 为主键；Lease 与 Mutation Plan 各自唯一；
- `BEGIN IMMEDIATE` 使并发同内容 finalize 收敛为同一行；
- 同 Lease/Plan 的不同 Receipt 返回 `mutation_receipt_conflict`，不覆盖旧 artifact；
- 读取时重新验证 JSON、Receipt identity、索引列、时间和 256 KiB 上限；
- `get()`、`get_by_lease()`、`list_recent()` 只返回通过完整模型校验的 Receipt。

`AgentEngine` 组合共享 Store 与 Service，但当前不注册 Slash Command 或 Agent Tool。Receipt 是内部权限门
之间的 artifact，不应在 EVO-03 验证执行器完成前成为一条可绕过编排的公开写入命令。

## 验收证据

- 真实单文件 Git worktree 完成 Writer 后生成 Receipt；重复 finalize identity 完全相同；
- 真实双文件 Patch Set 生成按路径排序的两项 diff/API facts；
- 主工作树保持原字节和 clean，Receipt JSON 不含 proposed source 或 worktree 绝对路径；
- 未 committed 写入、committed 后字节漂移、rationale secret 均返回 typed failure；
- Receipt 嵌套 file fact 篡改与 SQLite JSON 篡改均 fail-closed；
- 两线程并发写同一 Receipt 只形成一行，不同 Receipt 不得占用同一 Lease/Plan；
- 原单/多文件 Writer 成功路径和 `AgentEngine` composition 聚焦回归通过。

## 当前不足与 2.7b

本切片的 `tool_evidence` 证明 Static Guard、Writer 和 Postflight 三段机械治理，但 proposed contents 目前由
Writer 调用方传入，尚未绑定生成该内容的原始模型/tool-call event、run receipt 与参数摘要。为避免把
“治理链完整”误写成“生成来源完整”，EVO-02 继续保持 partial。

EVO-02.7b 必须把 mutation-generation trace 与 Plan 的 mutation stage 对齐：

- 只接受 Plan 允许的 `file_edit/file_write`，路径集合必须等于 approved scope；
- 记录 run/tool-call identity、脱敏参数 digest、结果 digest、顺序、失败与重试；
- 实际成功 tool-call 数不得超过 Plan budget，不能由模型总结文本替代；
- trace 必须进入 Mutation Receipt 新版本或独立不可变 artifact，并保持旧 v1 Receipt 可读；
- 完成后再评估 EVO-02 是否满足进入 EVO-03.1 Validation Plan 的阶段门。
