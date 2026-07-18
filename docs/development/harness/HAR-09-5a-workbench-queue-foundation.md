# HAR-09.5a Workbench Review Queue Foundation

## 目标

把 HAR-09.4a 的可信 `EvolutionProposalPreview` 通过一次显式操作写入现有 Workbench Proposal
队列，并保留完整来源、幂等身份和审计事件。本切片只建立队列入口，不改变 Proposal 的人工治理状态，
也不授予实验或代码执行权限。

## 持久来源契约

`workbench_proposals` 在原有字段之外保存：

- `source_kind=evolution_candidate`；
- Candidate ID、revision 和完整 SHA-256；
- Preview Proposal ID、generator version 和六类 Proposal kind；
- `idempotency_key=evolution:<preview-id>`。

手动 Proposal 的来源固定为 `manual`，不得夹带自动来源字段。Evolution 来源必须同时通过 Candidate
ID、Preview ID、SHA-256、revision、generator version、kind 和幂等键格式校验。来源不会编码到
`title`、`questions` 或其他展示文本中。

旧数据库使用 additive migration 增加字段，并创建 `(session_id, idempotency_key)` 条件唯一索引；空
幂等键的历史手动 Proposal 不受影响。

## 显式入队链路

同一个 `EvolutionProposalQueueAdapter` 服务三个入口：

- 用户斜杠命令：`/evolution enqueue <candidate-id> --mission <id> --task <id> [--agent <name>]`；
- Agent Tool：`evolution_proposal_queue(candidate_id=..., mission_id=..., task_id=...)`；只读的
  `evolution_candidates` 保持独立，避免把查询误标为写操作；
- New UI：解析同一斜杠命令，经 typed `evolution/review/request` 发送到 Bridge。

Adapter 每次重新从用户状态库读取 Candidate，并调用 HAR-09.4a generator 得到当前 Preview；客户端
不能上传或替换 Preview 内容。目标必须是当前 session 中、指定 mission 下的已跟踪 Workbench issue，
避免把 Proposal 绑定到虚构任务。

`detail` 仍为只读且不会自动入队。只有明确的 `enqueue` 动作产生写入。

## 幂等与审计

- 同一 session、同一 Preview ID 的并发入队只创建一条 Workbench Proposal；
- 重复操作即使由另一提交者发起，也返回同一个 Workbench ID，并显示“已在队列”；首次提交者保留在
  原记录中；
- 相同幂等键若携带不同 mission、task、标题、验证计划或来源，拒绝覆盖；
- 只有首次插入产生 `proposal.created`，审计 payload 保存非敏感来源身份；
- Candidate 新 revision 会生成新的 Preview ID，因此允许形成新的审阅项；其重复提醒与冷却规则由
  HAR-09.5b1 管理，详见 `HAR-09-5b1-governance-cooldown.md`。

## 安全与权限

- 入队结果始终为 `state=open`，只表示等待人类治理；
- 不创建实验、worktree、代码修改或验证运行；
- bypass 不跳过来源校验、issue 绑定、唯一索引或审计；
- Candidate 原始用户文本、Evidence payload、stdout、secret 和绝对工作区路径不会写入 Proposal；
- 参数 ID 长度限制为 1..128，拒绝换行、NUL 等控制字符。

## 验收标准与证据

- 真实 Feedback Intake 连续两次反馈形成 review-ready Candidate，并成功入队；
- Proposal round-trip 后来源 revision、digest、Preview ID、kind 和幂等键完整；
- 8 路并发重复入队最终只有 1 条 Proposal 和 1 条 `proposal.created`；
- 同键不同内容、手动来源伪造、未就绪 Candidate、不存在 Candidate、错误 mission/task 绑定均拒绝；
- 旧版 `workbench_proposals` 表可无损增加来源列和唯一索引；
- slash、Agent Tool 与 New UI 都调用相同 Adapter，并向用户明确显示“仍需人工决定，不可执行”。

## 明确未包含

- Workbench Reviews 页面中的 Proposal 列表和键盘决策操作；
- approved Proposal 到 EVO-02 Experiment Contract 的转换；
- HAR-09.6 before/after outcome tracking。

状态机与冷却已由 HAR-09.5b1 补齐；其余属于 HAR-09.5b2 及后续切片，不能由本切片的 `open`
状态推断为已完成。
