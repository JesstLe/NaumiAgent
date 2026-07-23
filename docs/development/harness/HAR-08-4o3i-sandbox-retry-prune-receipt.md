# HAR-08.4o3i Sandbox Retry Prune Receipt Authority

## 1. 交付结论

本切片把 HAR-08.4o3h 的只读 retention candidate 升级为可审计、不可变、一次性的 prune
authorization receipt。它只签发“未来可以尝试清理”的治理权威，**不删除、归档或修改任何 retry
事实**。

共享入口：

```text
/harness eval sandbox retry-prune-authorize <candidate-id> \
  --preview <preview-id> --preview-sha256 <sha256> \
  --candidate-sha256 <sha256> \
  --retry-action <retry-action-id> --dispatch <dispatch-id> \
  --epoch <n> --dispatch-sha256 <sha256> --updated-at <ISO8601> \
  --refs-sha256 <sha256> --preview-assessed-at <ISO8601> \
  --retention-days <n> --limit <n> --scan-limit <n> --reason <原因>
```

Agent Tool、New UI 与 Textual TUI 复用同一 Service、Store 和 Markdown renderer。Retention
preview 会显示可复制的完整签发命令。

## 2. 权限语义

- Tool 名称：`harness_eval_sandbox_retry_prune_authorize`；
- `read_only=false`，因为会新增审计回执；
- `destructive=false`，因为不会删除任何事实；
- `concurrency_safe=true`；
- 风险等级为 `medium`，在 permissive/moderate/strict/bypass 下允许；
- `requires_confirmation=false`，命令中完整 candidate fence 与显式 `reason` 就是本次意图；
- `requires_persistent_authorization=true`，即使没有下游委托也必须先落盘当前 Tool 权限回执；
- 单会话最多 50 次；lockdown 仍拒绝。

权限回执必须精确绑定完整 Tool arguments、tool name 与 run ID。Service 不接受缺失或参数摘要不同的父
回执。`bypass` 表示全权限直通，不再增加高风险二次确认，但仍保留持久审计链。

## 3. 三层重新校验

签发不是对 preview 的盲信：

1. Service 使用原 `preview_assessed_at + policy` 重建 4o3h preview；
2. 重新匹配 preview ID/SHA、candidate ID/SHA 与调用方提交的 dispatch fence；
3. Store 在 `BEGIN IMMEDIATE` 中重新读取 dispatch、retry/cancel receipt、Request Manifest、
   source/current ticket 和连续 H5a，核验：
   - 当前仍为已对账终态；
   - dispatch ID、epoch、request SHA、updated_at 未漂移；
   - 完整 protection refs SHA 未变化；
   - 同 candidate 尚未存在 accepted receipt。

preview 不匹配、候选消失、fence 漂移或重复授权都会写入稳定的 rejected receipt，而不是静默失败。
损坏的权威链或数据库摘要则 fail closed，不伪造拒绝事实。

## 4. 持久权威

Store schema v22 新增 `harness_sandbox_retry_prune_attempts`：

- 主键：`workspace_root + action_id`；
- accepted candidate 使用局部唯一索引，保证并发意图最多一个成功；
- action SHA 绑定 workspace、preview、candidate、dispatch fence、父权限回执、actor 和 reason；
- receipt SHA 额外绑定 decision、code 与创建时间；
- `hsrpa_` action ID 和 `hsrpr_` receipt ID 都进行格式及摘要重算；
- 同 action/同请求幂等返回；同 action/不同请求冲突；
- 读取回执时重新验证 action/receipt digest，篡改后 fail closed。

accepted 与 rejected 都是审计事实。表中不包含 execution authority，也不授予当前调用删除能力。

## 5. 用户体验

回执明确展示：

- 已签发或已拒绝及稳定错误码；
- preview/candidate ID 与 SHA；
- retry action、dispatch、epoch、request SHA、updated_at；
- protection refs SHA；
- 父权限回执 ID/SHA、actor、reason、签发时间；
- receipt ID/SHA。

accepted 文案明确“本轮没有删除任何记录”；rejected 文案明确“所有 retry 事实保持不变”。Renderer
不暴露 workspace 路径、owner 或 execution authority。

## 6. 验收标准

- 真实 Git workspace + SQLite 终态 dispatch 可签发 accepted receipt；
- 签发前后除 prune attempt 表外，所有 Harness facts 逐表逐行完全一致；
- 同 action 重放幂等；
- preview 摘要漂移产生 durable rejected receipt；
- 当前 dispatch 漂移不能获得 accepted receipt；
- 两个并发 action 争用同 candidate 时恰好一个 accepted、一个 already-authorized；
- 手工篡改 receipt SHA 后读取失败关闭；
- Tool 为非只读、非 destructive、无需二次确认且支持并发；
- shared Slash 同时供 New UI/TUI 使用；
- 定向 ruff、compileall 与相关 pytest 通过。

## 7. 自我审视与边界

本切片没有把“签发成功”伪装成“清理完成”。它仍有明确边界：

- 不删除任何 dispatch、receipt、ticket、request 或 H5a；
- 不估算释放空间；
- 不提供批量无限签发；
- 不跨 workspace 复用 candidate；
- 不允许 receipt 自己绕过未来状态校验。

HAR-08.4o3j 已实现 receipt consumption 与原子 prune execution；本 4o3i 回执自身仍不删除任何事实。
4o3j 会重新校验 accepted receipt、当前 protection refs 和共享引用，在一个事务内按依赖顺序删除并
生成独立执行回执；详见 `HAR-08-4o3j-sandbox-retry-prune-execution.md`。
