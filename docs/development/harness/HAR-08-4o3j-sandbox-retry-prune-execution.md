# HAR-08.4o3j Sandbox Retry Prune Execution

## 1. 交付结论

本切片把 HAR-08.4o3i 的 accepted prune authorization receipt 变成可执行、一次性且可审计的
原子清理权威。它不接受 preview 直接执行，也不扩大为批量垃圾回收。

共享入口：

```text
/harness eval sandbox retry-prune-execute <authorization-action-id> \
  --receipt <authorization-receipt-id> \
  --receipt-sha256 <authorization-receipt-sha256> \
  --candidate <candidate-id> --candidate-sha256 <candidate-sha256> \
  --retry-action <retry-action-id> --dispatch <dispatch-id> \
  --refs-sha256 <protection-refs-sha256> --reason <原因>
```

Agent Tool、New UI 与 Textual TUI 复用同一 Slash parser、Service、Store 和 Markdown renderer。
accepted 4o3i 回执直接给出可复制的完整执行命令。

## 2. 权威链与权限语义

- Tool：`harness_eval_sandbox_retry_prune_execute`；
- `read_only=false`、`destructive=true`、`concurrency_safe=true`；
- `requires_confirmation=true`，普通模式只进行一次高风险确认；
- `requires_persistent_authorization=true`，执行前必须生成绑定完整 arguments、tool name 与 run ID 的
  当前权限回执；
- 风险等级 `high`，单会话最多 20 次；
- permissive/moderate/strict 允许，lockdown 拒绝；
- bypass 是全权限直通，不弹出高风险确认，但仍写入持久权限与执行审计回执。

Service 不接受参数摘要、tool name、run ID 或执行权威不一致的父权限回执。4o3i 授权回执与本次权限
回执是两个不同层次：前者批准精确候选，后者记录当前执行调用。

## 3. 事务内重新校验

Store v23 使用 `BEGIN IMMEDIATE`，在任何删除发生前依次验证：

1. execution action 是否为同请求幂等重放；
2. 4o3i action/receipt 是否存在且摘要可重新计算；
3. authorization receipt 是否为 accepted；
4. receipt/candidate/retry/dispatch/protection refs 是否与调用参数逐项相同；
5. authorization receipt 是否已被其他 executed receipt 消费；
6. dispatch 是否仍存在且为 completed/failed/cancelled；
7. dispatch epoch、request SHA、updated_at 是否与授权 fence 相同；
8. cancel/retry/Request Manifest/source ticket/current ticket/H5a 权威链是否完整；
9. 重新生成 detail snapshot 后，完整 protection refs SHA 是否仍与授权一致。

缺失、拒绝、参数不匹配、已消费和候选漂移都生成稳定 rejected execution receipt，删除计数保持 0。
任何权威摘要损坏直接 fail closed，不伪造普通拒绝回执。

## 4. 最小安全删除集

校验成功后，在同一事务按依赖顺序删除：

1. 精确 terminal dispatch；
2. 精确 accepted retry attempt/receipt；
3. 仅当不存在任何其他 cancel attempt、retry source 或 dispatch 引用时，删除当前 terminal ticket。

下列事实默认保留：

- 4o3i authorization attempt/receipt；
- execution attempt/receipt；
- Request Manifest；
- source ticket；
- cancel receipt；
- H5a 连续样本；
- 被其他审计或恢复事实引用的当前 ticket。

因此本切片不是“整 cohort 物理清空”。它只移除已经终结且不再提供恢复入口的最小安全集，同时保留
重放、审计、Baseline/Comparison 和后续归因所需事实。

任一 DELETE row count 不为 1、SQLite trigger/IO 失败或回执写入失败都会回滚整个事务，不允许
“dispatch 已删但 retry receipt 尚在”之类的半完成状态。

## 5. 不可变执行回执

`harness_sandbox_retry_prune_executions` 保存：

- `hsrpe_` execution action 与 `hsrper_` receipt；
- authorization action/receipt ID 与 SHA；
- candidate/retry/dispatch/protection refs fence；
- 被删除 retry receipt 对应的 cancel receipt 一次性消费 tombstone；
- 当前父权限回执；
- `executed|rejected` 与稳定 code；
- dispatch、retry attempt、ticket 删除计数；
- 明确保留的 protection reference 数量；
- actor、reason、created_at；
- action SHA 与 receipt SHA。

同 action/同请求幂等返回；同 action/不同请求冲突。accepted authorization receipt 通过 partial unique
index 最多产生一个 executed receipt；cancel receipt tombstone 也有 workspace 内唯一约束。后续 retry
授权会同时检查仍存在的 accepted retry attempt 与 executed prune tombstone，因此删除 retry 行不会恢复
cancel receipt 的消费能力。读取时重新计算 action/receipt 摘要、ID、decision/code 和所有计数；篡改后
失败关闭。

## 6. 用户体验

授权回执显示下一步可复制命令。执行回执明确区分：

- 已完成：显示原子事务已提交、三类删除计数、保留引用数及默认保留事实；
- 已拒绝：显示稳定错误码，并明确事务未执行、删除计数均为 0。

Renderer 不泄露 workspace 路径、owner、execution authority key 或 secret。New UI 与 TUI 不各自推导
删除状态，只消费同一工具结果。

## 7. 验收证据

- 真实 Git workspace + SQLite：dispatch、retry attempt 和无共享引用 ticket 精确删除；
- Request Manifest、source ticket、cancel receipt、H5a 和授权回执保持存在；
- 同 action 重放返回同一 execution receipt；
- prune 后再次提交原 cancel receipt 仍被判定为 consumed；
- dispatch updated_at 漂移产生 durable rejected receipt，零删除；
- 当前 ticket 存在后续 cancel 审计引用时保留；
- 注入第二步 DELETE 失败后，第一步 DELETE 与 execution receipt 一并回滚；
- 两个并发 action 消费同一 authorization receipt 时，恰好一个 executed、一个 already-executed；
- 手工篡改 execution receipt SHA 后读取失败关闭；
- Tool metadata、普通模式一次确认、bypass 零确认和 lockdown 拒绝均有定向测试；
- shared Slash 在真实 Engine bypass 模式完成清理；
- 定向 Ruff、py_compile 和相关 pytest 通过，不以全量测试代替模块证据。

## 8. 自我审视与剩余边界

本实现没有把保守删除伪装成全量回收：

- 没有删除 Request Manifest、H5a、cancel chain 或 source ticket；
- 没有计算 SQLite page reclamation 或磁盘释放量；
- 没有跨 workspace、跨主机批量执行；
- 没有后台自动 prune worker；
- 没有 Linux/Windows 隔离 CI 证据。

后续若要继续释放共享事实，必须先建立显式引用图、Baseline/Comparison 外键审计与独立 retention
policy；不得通过扩大本事务的 DELETE 列表实现。
