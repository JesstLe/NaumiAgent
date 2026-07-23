# UI-18.4d2 宿主绑定的 Interaction Takeover

## 目标

把 UI-18.4d1 已显示的“可接管”事实收口为真实用户闭环：用户执行
`/goal interaction takeover <id>` 后，不是只在 Harness Store 中更换 owner，而是必须同时把
同一个 interaction 绑定到当前 New UI Bridge 的 Future/卡片，或 Textual TUI 的 Modal/续租
任务。只有当前 owner lease 已过期且问题 deadline 未到时才能接管。

## 共享权威原语

`DurableInteractionAuthorityClient.claim(interaction_id)` 是 Bridge 与 TUI 唯一的精确接管原语：

1. 从 workspace-scoped Harness Store 重读目标 interaction，不信任列表快照的 sequence/owner。
2. 记录不存在或已是终态时 fail closed。
3. 问题 deadline 已到时先执行 sequence-fenced expire，不允许 takeover。
4. foreign owner lease 仍有效时拒绝并给出有界剩余秒数，不抢占活跃界面。
5. lease 过期时通过 Harness Store `expected_sequence` 事务提交 takeover；foreign owner 的 epoch
   单调增加，same owner 只续租不虚增 epoch。
6. 并发冲突保留 Store 错误，宿主展示“接管前已变化”，不伪造成功。

## New UI Bridge 绑定

- 新协议事件 `interaction_takeover` 只携带严格 `ask-*` ID，登记为 experimental control/audit
  事件。
- Bridge 先验证 interaction 属于当前 workspace Goal 已关联的 Pursuit，拒绝 runtime/tool/
  其他 Goal 记录。
- exact claim 成功后，Bridge 创建 replay-only Future、注册 `PendingInteraction`、发送同 ID
  `interaction/request`、启动 owner renewal 和 deadline timeout。
- 前端因此立即打开真实结构化交互卡；答案仍经 `interaction_response` 和 owner/epoch/
  sequence fencing 提交，不增加第二条回答路径。
- 自动 startup replay 与手动 takeover 共用 `_interaction_claim_lock`，同 ID 并发操作只能展示一
  张卡。
- 若 exact claim 后 Bridge 输出通道在卡片绑定阶段失败，必须移除本地 pending、停止 renewal/
  timeout task 并返回 `interaction_takeover_bind_failed`；不能留下一个不可见但持续续租的问题。
  已取得的短租约自然过期后可由活动宿主重试。

## Textual TUI 绑定

- 共享 slash dispatcher 只在存在活跃 frontend host 且其实现 `takeover_goal_interaction()` 时
  接受 takeover；无交互宿主的 legacy CLI 明确拒绝。
- TUI 对 Goal/Pursuit 归属做同样校验，exact claim 成功后把记录放入 active ID 集合、启动
  renewal，然后使用既有 `UserInteractionScreen` 立即展示。
- 手动接管和自动 recovery 共用 `_interaction_claim_lock`、`_interaction_lock` 与
  `_complete_claimed_interaction()`，不复制第二套 Modal/回答/超时逻辑。
- 回答持久化后提示用户对 Goal/Pursuit 显式执行 `/pursue resume`，不隐式启动新的
  模型轮次。

## 快照与兼容

- Goal interaction 公开投影新增 `can_takeover`，只有 pending + live question + expired owner lease
  才是 `true`。
- New UI 只在 `can_takeover=true` 时显示接管命令；已回答、已超时、已取消均不显示。
- 新前端读取旧 Bridge v1 snapshot 时，缺失 `can_takeover` 按 `false` 降级，不把未知状态
  误判为可操作。
- 接管后收到 `interaction/request` 时本地立即清除 `can_takeover`；终态 receipt 同时清除
  cancel/takeover 动作。

## 验收标准

- exact claim 定向测试覆盖 live foreign owner 拒绝、expired foreign owner epoch+1 与 deadline-first
  expire。
- Bridge 真实 SQLite Store 测试证明手动接管后产生一个 `interaction/request`、当前 owner/
  epoch 正确，且结构化回答可进入 answered 终态。
- Bridge 并发两次手动 takeover 只展示一张卡，另一次返回 `interaction_already_active`。
- Bridge 卡片输出失败会清理本地 pending/task、返回 typed error，且不会伪造成功或继续隐形续租。
- TUI 真实 Textual `run_test` 证明 exact claim 会立即打开 `UserInteractionScreen`，用户选择后
  authority 保存 answered 和标准化答案。
- Python/Node 双端协议严格验证 ID，协议合同和事件治理注册表完全覆盖新事件。
- Goal snapshot/renderer 和 Node state/component 证明只在权威条件成立时显示 takeover。

## 保留边界

- 手动 takeover 不能抢占 live owner；本切片不增加“强制踢掉活跃界面”超级权限。
- Goal 页仍为命令驱动动作，页内选中/展开详情与按键操作属于后续 UI-18.4d3。
- interaction 历史仍是最近 50 项，cursor/筛选/优先级未完成。
- 回答已提交但 Pursuit checkpoint 未消费时，仍由既有 reconcile 要求显式 resume。
