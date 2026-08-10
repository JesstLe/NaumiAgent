# HAR-10.8f2h Pursuit 终态 Outbox 有效状态与处置历史

## 目标

HAR-10.8f2g 用 append-only abandon authority 表达“停止投递”，但底层 delivery event chain 为兼容历史仍显示
`pending`。如果调用方直接读取旧 state，就可能把已经放弃的记录误判为待处理。本切片建立唯一的认证
effective-state 读取边界，并把 abandoned 事实作为有界、脱敏的 disposed history 投影到 Goal Tool、CLI、
Textual TUI 与 New UI。

本切片不修改历史 outbox canonical JSON，不把 abandon 伪造成 delivered，也不开始 retention apply。

## 统一 effective-state

`PursuitStore.get_terminal_outbox_effective_state()` 同时认证：

- outbox snapshot 与 append-only outbox event chain；
- dispatch snapshot 与 dispatch event chain；
- failure head、完整 bounded failure chain 及其 prior hash；
- requeue/abandon receipt 与原 failure、idle dispatch、时间和摘要绑定。

只有以下三种结果可以离开 Store：

| delivery state | disposition authority | effective-state | 语义 |
|---|---|---|---|
| `pending` | 无 abandon | `pending` | 仍可能到期、退避、认领或等待人工审查 |
| `delivered` | 禁止 abandon | `delivered` | recovery attempt 终态已经成功交付 |
| `pending` | 认证 abandon receipt | `abandoned` | 未交付，但已由受控处置永久撤销领取资格 |

`delivered + abandon`、多个 abandon、abandon 与非 idle dispatch、receipt 与最新 failure 不一致都会失败关闭。
调用方不再需要理解 SQLite overlay 细节，也不得自行使用 `outbox.state` 推导调度状态。

## 可认证 disposed catalog

`terminal_outbox_disposed_catalog(limit, scan_limit)` 从 failure head、failure events 与 abandon authority 的并集
发现候选 outbox。它先认证完整 authority，再选择 effective-state 为 `abandoned` 的记录，按
`(abandoned_at, receipt_id)` 逆序稳定排列。

- `limit` 为 `1..100`；`scan_limit` 不超过 10000；
- 超过 authority 扫描上限时拒绝返回不完整的“全量”结论；
- 返回 `records / total / truncated`，Goal 当前消费最新 20 项；
- 每项 Store 记录保留 outbox、failure、receipt，供后续 retention preview 做引用校验；
- 删除或篡改可发现的 receipt/failure/head 会破坏认证，不会静默隐藏为正常 pending。

## 用户与 Agent 投影

Goal terminal-outbox 子协议升级为 schema v4，并兼容读取 v1-v3。公开 disposed item 仅包含：

- `ptabn_...` receipt ID 与 `ptfail_...` dead-letter ID；
- 固定 `effective_state=abandoned`；
- 四种受控 reason；
- failure code、failure sequence、abandoned time。

outbox/run/attempt/owner、source request、failure/dispatch/receipt digest 不进入公开投影。CLI 与 Textual TUI 通过
`/goal` 的共享 Markdown renderer 展示同一历史；Agent Goal Tool 返回同一 typed snapshot；New UI 通过协议
normalizer 复验 ID、枚举、时区、数量和截断关系。

颜色语义保持稳定：活动死信使用红/黄表示风险与可操作选择；已处置历史使用绿表示已完成处置、青色标题表示
审计区域、灰色显示 effective-state/sequence/receipt 等次要证据，避免把历史处置继续渲染成活动故障。

## 验收标准

- 真实 SQLite pending、delivered、abandoned 三类记录得到唯一且正确的 effective-state；
- abandon 后重开 Store，disposed catalog 仍返回同一认证 receipt/failure；
- abandon 不增加 delivered 计数，底层 delivery state 仍如实为 pending；
- receipt payload 篡改同时阻断 exact receipt、effective-state、active catalog 与 disposed catalog；
- disposed catalog 有硬 limit/scan limit、稳定排序、total/truncated 事实；
- Goal schema v4 不泄露内部 identity/digest，v1-v3 输入规范化为安全 v4 默认；
- CLI、Textual TUI、Agent Tool、New UI 显示同源处置历史；
- New UI 对活动死信与已处置历史使用不同语义色彩；
- 仅运行 Store、Goal Tool/投影和 New UI protocol/state/render 小模块测试。

## 本轮验证

- `tests/unit/test_pursuit_terminal_outbox_dead_letter.py`：真实 SQLite 状态区分、重开、篡改与有界目录；
- `tests/unit/test_goal_panel.py`：schema v4、身份脱敏、CLI/TUI renderer；
- `tests/unit/test_goal_tools.py`：Agent Tool 继续消费共享 Goal authority；
- `frontend/terminal-ui/test/protocol.test.js`：v1-v4 兼容、strict disposed normalization、私有字段剔除；
- `frontend/terminal-ui/test/goal-pursuit-page.test.js`：处置历史与颜色语义渲染；
- `frontend/terminal-ui/test/state.test.js`：Goal 路由和既有写动作回归。

## 自我审视与未完成边界

- 本切片提供最新 20 项的有界历史和 `truncated`，尚未提供用户可导航的 cursor 翻页；不能称完整审计浏览器；
- 全局 authority 仍以单个 SQLite Store 为信任域；若攻击者同时删除 outbox、failure head/events 与 receipt 的全部
  可发现行，当前没有外部 Merkle anchor 证明曾经存在；
- HAR-10.8f2i 已基于 disposed history 交付 retention protection graph 与只读 preview receipt；apply 仍未实现；
- push stream、跨 Store 原子 terminal commit、kill-at-every-write-point 与 24 小时 soak 仍未完成。

下一最小切片应先实现 retention apply admission、引用重新认证、可恢复变更计划与逐写点故障矩阵；在这些边界
充分验证前，不得实现物理删除。
