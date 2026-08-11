# HAR-10.8f2j Pursuit 终态 Outbox retention apply 准入

## 目标

HAR-10.8f2i 证明某组 abandoned outbox 在固定时间和策略下进入只读候选集，但 preview 本身永远不是删除权限。
本切片实现物理 prune 之前的真实准入边界：

- 精确绑定 `preview_id + preview_sha256 + assessed_at + policy`；
- 在一个 `BEGIN IMMEDIATE` 事务内重新认证 effective-state、全部 protection refs 与候选顺序；
- 从真实 SQLite 行生成确定性的删除写点和私有恢复快照；
- 持久化内容可认证、幂等且不受未来 outbox 级联删除影响的 admission；
- 为每个计划写点分配 before/after killpoint 与事务回滚动作；
- 通过 Agent Tool、CLI、Textual TUI 与 New UI 共用 Slash 通道返回同一中文回执。

本切片仍不执行计划，不删除、归档、压缩或改写任何 outbox 事实。所有 admitted 回执固定声明
`execution_authority=false` 与 `physical_prune_authority=false`。

## 精确 Preview 绑定与拒绝决策

用户从 `/pursue outbox retention-preview` 获得 ID、完整 SHA、固定评估时间与策略，再显式请求：

```text
/pursue outbox retention-admit <ptorpv_...> <sha256> \
  --assessed-at <带时区 ISO> \
  [--retention-days 30] [--limit 20] [--scan-limit 100]
```

Store 不信任调用方提交的候选或引用列表。它在写锁事务中使用相同策略重新构建 preview；只有新旧 ID/SHA
完全一致时才继续。以下情况返回 strict、内容寻址的 `rejected` 决策且不持久化 admission：

| reason | 条件 | 用户动作 |
|---|---|---|
| `preview_authority_changed` | preview SHA、候选集合、计数、顺序或任一保护引用变化 | 重新运行 preview 并人工审阅 |
| `no_candidates` | 精确 preview 有效，但没有候选 | 无需执行；等待下一评估周期 |

格式错误、无时区时间、越界策略和 ID/SHA 前缀不一致在进入 Store 前拒绝；错误消息不回显输入或私有 identity。

## 可恢复变更计划

对每个候选，Store 从真实表读取将来 prune 可能触及的全部行，按外键安全顺序生成有界步骤：

1. requeue receipts（存在时）；
2. abandon receipt；
3. failure head；
4. failure events；
5. dispatch events；
6. dispatch snapshot；
7. outbox events；
8. outbox snapshot。

每一步绑定 operation、实际行数、`exact_authenticated_snapshot` 前置条件、
`rollback_candidate_transaction` 恢复动作、step SHA 以及唯一 before/after killpoint。空表不生成虚假写点。
候选计划还绑定原 candidate SHA、protection refs SHA、私有恢复快照 SHA 和总恢复行数。

私有 `recovery_json` 保存列名与原始 SQLite 值，足以让后续执行器在同一 schema 下机械恢复；公开 admission 只暴露
摘要和计数，不公开 outbox/run/attempt/event identity、绝对工作区或 payload。恢复快照集合摘要被公开回执绑定，任何
payload、索引列或私有快照修改都会使读取失败关闭。

## 持久化与并发不变量

`pursuit_terminal_outbox_retention_admissions` 不引用 outbox 外键，因此未来 prune 不会级联删除恢复 authority。表对
preview SHA 和 source-request SHA 分别设置唯一约束：

- 同一 preview 重试返回已有且重新认证后的同一 admission；
- 同一 source request 不能绑定不同 preview/恢复快照；
- admission insert 与快照写入是一个 SQLite 原子写入；
- `before_admission_insert` 和 `after_admission_insert` 任一故障均整体回滚；
- 返回 admitted 前必须 commit，重开 Store 可重新认证读取。

计划中的未来 delete 写点均有 before/after killpoint，但本切片不执行这些写点。HAR-10.8f2k 必须逐点注入故障，证明
候选事务在 commit 前全部回滚、commit 后由 durable admission/tombstone 收口，才可宣称物理 prune 可恢复。

## 双通道与权限

Agent Tool：

```text
pursuit_terminal_outbox_retention_admission
```

Tool 使用严格 JSON Schema，写入 admission authority，标记 `concurrency_safe=true`、`read_only=false`。它不要求二次
确认，但不在 lockdown 模式开放；bypass 为全权限直通。CLI、兼容 CLI 与 Textual TUI 使用同一个严格参数 parser，
New UI 只把原始 Slash 命令发送给后端，不在前端复制准入或计划逻辑。

## 验收标准

- 真实 SQLite abandoned 候选生成包含真实行数、确定顺序与完整 killpoint 对的恢复计划；
- admitted 前在写事务内重新认证完整 preview/protection graph；错误 SHA 返回结构化拒绝且不写 admission；
- 空候选返回 `no_candidates` 且不写 admission；
- admission 重试幂等，重开 Store 返回同一 strict receipt；
- before/after admission insert 故障均不留下半条记录，outbox 仍为 authenticated abandoned；
- 修改公开 receipt 或私有 recovery snapshot 后读取失败关闭；
- 公开 JSON/Markdown 不包含 outbox identity，私有快照保留未来机械恢复所需事实；
- Agent Tool 与 Engine 共用同一 Store 方法，CLI、Textual TUI、New UI 共用同一 Slash/Tool 通道；
- 不运行全量测试，只运行 retention、CLI/TUI 路由与 New UI Slash 小模块测试。

## 自我审视与未完成边界

- admission 是非执行 authority；当前没有任何 API 消费计划或执行 DELETE；
- 恢复快照位于同一 SQLite 信任域，还没有外部 Merkle anchor、离线备份或密钥封装；
- 当前恢复协议依赖相同 schema；物理 prune 前需要 schema-versioned restore validator 和 tombstone；
- 计划声明未来写点的故障矩阵，但只有 admission 自身的 insert 已做真实故障注入；delete 写点必须在执行切片逐点验证；
- disposed history/preview 尚无 cursor 翻页，push stream、跨 Store 原子 terminal commit 与长时 soak 仍未完成。

下一最小切片为 HAR-10.8f2k：实现 admission 消费器、不可变 prune tombstone、逐候选原子执行、每个 delete 写点的
before/after 故障注入与机械恢复。它必须保持默认 dry-run，只有精确 admission authority 才能进入显式 apply。
