# HAR-10.4a Pursuit 持久 Checkpoint 核心

## 目标

在不声称已经支持跨进程继续执行的前提下，为 Pursuit 建立可恢复、可校验、有界的权威 checkpoint。
它补足 `PursuitRun` 状态摘要无法重建 GoalSpec、成功标准、预算、迭代历史和等待工作的缺口，并为
HAR-10.4b resume executor 提供稳定输入。

## 权威合同

`PursuitCheckpoint` 使用严格、冻结、拒绝额外字段的 Pydantic schema。schema v1 保存：

- run ID、单调 sequence、创建时间、状态、阶段和轮次；
- 原始目标、完成描述、复杂度、约束键和完整成功标准；
- 当前 todo、下一行动、证据游标、后台等待项和 worktree；
- 已消耗 token、成本、时间及三类预算上限；
- 最近 20 轮有界历史，用于后续恢复停滞判断；
- `pending_interaction` 的类型化占位，供 HAR-10.6 接入。

所有字符串在进入 checkpoint 前截断；常见 API key、token、password、secret 和 `sk-*` 形态会脱敏。
不保存原始大工具输出、reasoning 或无限历史。无限预算编码为 `null`，规范 JSON 禁止 NaN/Infinity。

## 存储与完整性

`pursuit_checkpoints` 是 `pursuit.db` 的向后兼容新增表，每个 run 只保存最新 checkpoint，避免小时级运行
无限增长。每行包含 canonical JSON、SHA-256、内容寻址 checkpoint ID、schema version 和 sequence。

写入规则：

1. run 必须已经存在；
2. sequence 只能递增；
3. 同 sequence 同摘要为幂等重试；
4. 同 sequence 不同内容或序号倒退为冲突；
5. SQLite foreign key 在每个连接启用。

读取时依次校验 payload 摘要、严格 schema、run ID、sequence、schema version 和 checkpoint ID。任一不一致
都 fail closed，恢复入口不会读取不可信状态。

## Pursuit 接入边界

checkpoint 只在 HAR-10.1b 已 fenced 的安全边界写入：

- GoalSpec 解析和 worktree 准备完成；
- 每轮 assessment 已进入历史；
- 普通/恢复 plan 已形成；
- action 结果已收集；
- waiting、verification 和 terminal 状态已确定。

checkpoint 保存失败会停止本次 Pursuit，不继续制造不可恢复的新副作用。终态 checkpoint 与最新
`PursuitRun` 的状态、证据游标保持一致。HarnessStore 与 PursuitStore 跨库原子提交仍不在本切片内。

## 恢复语义

- 旧 run 没有 checkpoint：保持 `blocked/checkpoint_required`；
- checkpoint 校验失败：拒绝恢复且不改写 run；
- checkpoint 有效：进入 `blocked/checkpoint_ready`，显示内容寻址 ID；
- resume 回收后台结果后以新 sequence 写入 reconciled checkpoint，旧 waiting 快照不会再次成为最新输入；
- reconciled checkpoint 写入失败：run 进入 `blocked/checkpoint_error`，旧快照不得续跑；
- 任何情况都不伪装为 `running`。

HAR-10.4a 只交付权威输入和安全判定。HAR-10.4b 才负责从 snapshot 重建 GoalSpec、历史、预算和下一安全
行动，并在新 lease epoch 下继续 planner loop。

## 验收证据

- 真实 SQLite 保存后用新 Store 实例重开，checkpoint 逐字段一致；
- 重复写幂等，序号倒退和同序号异内容被拒绝；
- payload 和 checkpoint ID 篡改均拒绝恢复；
- 未创建数据库时只读查询不产生目录或文件；
- 完整 leased Pursuit 阻塞路径保存 terminal checkpoint，包含 goal、criteria、历史与正确证据游标；
- resume 能区分 legacy missing 和 verified ready，且不显示虚假运行状态；
- Pursuit、lease 和 checkpoint 小模块测试通过。

## 当前不足与下一切片

HAR-10.4a 尚未恢复执行，也未解决 destructive action 的 durable idempotency、外部任务 reconcile、跨 Store
事务或人工交互回填。下一切片 HAR-10.4b 应只实现 checkpoint → 内存状态重建和受 lease 保护的继续执行；
随后再由 HAR-10.5 对 browser/background/process 的真实外部状态做 reconcile。
