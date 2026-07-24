# HAR-10.8b 长周期运行机械终态裁判

## 目标

把 Pursuit 中分散的 `completed`、`waiting`、`blocked`、`cancelled`、
`budget_exceeded` 分支收口为一个确定性、内容寻址、可持久审计的机械裁判。模型可以提出计划和
解释差距，但不能直接声明运行完成、等待或预算越界。

本切片承接 HAR-10.8a 的定向验证策略：10.8a 决定哪些验证命令可运行，10.8b 决定这些机械事实
允许运行进入哪个边界状态。

## 权威合同

`src/naumi_agent/orchestrator/pursuit_terminal.py` 定义：

- `PursuitBoundaryFacts`
  - 成功标准总数；
  - 已验证标准数；
  - 带强证据的标准数；
  - 最终定向验证状态；
  - 用户取消信号；
  - time/cost/iteration 预算越界；
  - background/interaction 等待引用及数量；
  - 空规划、停滞、无恢复行动等机械 blocker。
- `PursuitBoundaryDecision`
  - 内嵌用于本次裁判的完整、受限 `facts`；
  - 稳定 `status` 与 `code`；
  - 中文原因和明确下一步；
  - `terminal` 与 `resumable`；
  - `facts_sha256`，读取时与内嵌事实重新计算核对；
  - 对完整决策内容寻址的 `decision_id`。

所有模型 `extra=forbid` 且 frozen。计数矛盾、没有引用的 waiting、waiting 与 blocker 混合、
预算与等待混合、没有全部强证据却声称最终验证通过，都会在进入运行时前被拒绝。

## 裁判优先级

1. 用户取消；
2. time/cost/iteration 预算越界；
3. 有持久引用的 interaction/background waiting；
4. 明确 blocker；
5. 全部标准有强证据且最终定向验证通过；
6. 最终验证失败或标准未完成时继续运行。

取消可以与同一时刻观察到的预算、waiting 或 blocker 共存，但取消优先，避免把用户意图错误
呈现成其他系统状态。没有取消时，最终验证、预算、waiting 和 blocker 的相互冲突组合失败关闭，
不依靠隐式优先级猜测。

`blocked` 是当前运行尝试的终态，但 `resumable=true` 表示经过人工核对后可以从受治理 checkpoint
显式恢复；它不是自动续跑许可。`waiting` 不是终态，必须保留可恢复 interaction 或 background
引用。

## Pursuit 生产接入

`GoalPursuitLoop` 已让下列真实路径消费同一裁判：

- 用户取消；
- 最大时间、最大成本、最大轮次；
- 完成前的强证据与最终定向验证；
- 空规划；
- 停滞且禁用恢复；
- 停滞但没有恢复行动；
- 后台任务等待；
- 用户交互等待。

行动返回 `waiting` 但没有 `_pending_background` 持久引用时，不再制造不可恢复 waiting，而是
`waiting_without_authority` blocked。

后台任务完成或用户交互回答后，如果运行持有完整 criterion 状态，会生成新的 running 裁判替换
过期 waiting 裁判；旧记录缺少 criterion 快照时只恢复运行，不伪造新的机械事实或历史裁判。

每次裁判会：

1. 在持有 run lease 的边界上应用；
2. 更新 Pursuit status、phase、blocked reason 与 next action；
3. 写入一条 `boundary_decision` 强证据；
4. 将完整严格决策写入 `pursuit_boundary_decisions`；
5. 与 checkpoint 一起越过持久提交边界。

## 持久化与防篡改

`PursuitStore` 新增 append-only `pursuit_boundary_decisions` 表：

- 主键为 `(run_id, decision_id)`；
- 保存含受限机械事实的完整严格 JSON、payload SHA-256 与记录时间；
- 相同 identity 对应不同 payload 时拒绝保存；
- 读取时复验 payload digest、Pydantic 合同和内容寻址 `decision_id`；
- `pursuit_runs.boundary_decision_id` 明确指向当前裁判，避免 running → waiting → 相同 running
  决策重现时被首次记录时间误导；
- 删除 PursuitRun 时由外键级联删除；
- `list_boundary_decisions()` 提供有界唯一决策审计集合。

`PursuitRun.boundary_decision` 保存最近一次已认证裁判，旧数据库在打开时通过
`CREATE TABLE IF NOT EXISTS` 增量获得该表，不改写已有运行记录。

## New UI 与 TUI

共享 Goal/Pursuit projection 增加严格、低敏的 `boundary_decision`：

- New UI Goal 页面按状态颜色显示 code、status、短 decision id 与原因；
- TUI/CLI fallback 的共享 Markdown 显示相同字段；
- JavaScript protocol 严格校验 schema、两个 SHA-256、code、status、
  terminal/resumable 一致性，并删除未知私有字段；
- UI 不重新计算或覆盖裁判。

## 验收标准

- 全部成功标准有强证据但未执行最终验证时只能得到
  `final_verification_required/running`；
- 最终验证失败不得完成；通过后得到 `completed_verified/completed`；
- time、cost、iteration 三类预算产生不同稳定 code；
- 用户取消优先于同时观察到的预算越界；
- background/interaction waiting 必须有精确引用；
- 无 waiting 引用时得到 `waiting_without_authority/blocked`；
- 空规划和两类停滞得到独立稳定 code；
- 决策在 Store 重开后保持一致，篡改 JSON 后读取失败；
- 旧 schema 自动增加并回填最近裁判指针，重复 decision identity 仍能恢复当前状态；
- Goal New UI、TUI/CLI fallback 展示同一最近裁判；
- 旧 Pursuit 记录没有裁判时保持兼容，不伪造历史。

## 定向验证

```bash
python3 -m pytest \
  tests/unit/test_pursuit_terminal.py \
  tests/unit/test_pursuit_terminal_runtime.py \
  tests/unit/test_pursuit.py \
  tests/unit/test_pursuit_checkpoint.py \
  tests/unit/test_pursuit_lease.py \
  tests/unit/test_pursuit_recovery.py \
  tests/unit/test_pursuit_action_ledger.py \
  tests/unit/test_goal_panel.py -q

node --test \
  --test-name-pattern='goal snapshot is strict, bounded, and preserves stable Pursuit links' \
  frontend/terminal-ui/test/protocol.test.js

node --test frontend/terminal-ui/test/goal-pursuit-page.test.js
```

## 自我审视与未完成项

- 裁判已进入真实 Pursuit 主循环，不是只生成报告的旁路工具。
- 决策表提供内容完整性和历史读取，但 PursuitRun、checkpoint 与 boundary decision 仍不是跨表单条
  SQL 事务；lease fencing 降低竞态，真正跨 Store 原子提交仍属于 HAR-10.1/10.4 后续。
- 恢复流程中的 `checkpoint_required`、`reconcile_required` 等历史分支尚未全部改用该裁判；本切片只
  接入新运行主循环、后台等待和 interaction 生产路径。
- `failed` 仍保留为旧 PursuitRun 兼容状态，但 HAR-10.8b 不生成新的 `failed` 裁判；可恢复失败应
  blocked，不可恢复预算/取消使用各自明确状态。
- 下一小切片应把 resume/reconcile/checkpoint-error 的阻塞分支接入同一裁判，再建立跨 Store 原子
  terminal commit；不应直接跳到 24 小时 soak。
