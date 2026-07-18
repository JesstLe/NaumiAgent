# HAR-09.1a 可信反馈信封与 Candidate Intake

## 目标与边界

本切片把用户纠正和缺陷报告转换为可审计、不可执行的 Evolution Candidate Draft，同时阻止
Agent 把自己的判断伪装成“用户反馈”。它复用 EVO-01 Candidate Store，不创建第二套候选库，
也不实现阈值晋升、Review Queue、自动修改或自动实验。

## 两类不可混淆来源

- `user_feedback`：用户显式输入 `/feedback`。来源 URI 是内部摘要引用，原始摘要、会话文本和
  secret 不写入 Evolution DB；相同 Session、分类、scope、topic、摘要在同一分钟内幂等。
- `agent_interpreted_feedback`：Agent Tool `feedback_intake` 只能在正在执行的 durable Chat Run
  内调用。Engine 在进入 streaming run 前签发包含真实 `run_id`、`user_message_id`、输入全文
  SHA-256 和开始时间的内存信封，结束或异常时立即清除。Tool 无法自己提供或覆盖这些字段。

两类 Evidence 可以按相同 `finding_code + scope + topic` 聚合，但 `source_kind` 永久保留，Review
策略可以分别赋权。它们证明“反馈确实被提交/解释过”，不证明报告中的产品判断机械正确；所有
Draft 固定 `experiment_eligible=false`。

## 分类与隐私

命令格式：

`/feedback <correction|defect|preference|cancel|praise> <scope> <topic> <摘要>`

- 只有 `correction` 和 `defect` 形成 Evidence；
- `preference`、`cancel`、`praise` 明确返回 ignored，不创建数据库，避免把 taste、取消或正向
  评价污染成 Agent 缺陷；
- scope 必须是无 `..`、无绝对路径的相对范围；topic 必须是稳定小写标识符；
- summary 只参与 SHA-256，不进入 Evidence、Candidate、事件或用户回执；
- provider/model/platform 作为每条 Evidence 的有限聚合维度保存，不参与根因 fingerprint。

## 双通道

- 用户：CLI、TUI、新 UI 均通过共享 slash router 执行 `/feedback`；
- Agent：注册 `feedback_intake` Tool，调用同一 `FeedbackIntakeService.ingest()`；无 active durable
  turn 时失败关闭；
- 两个入口最终均调用 `build_candidate_draft()` 和 `EvolutionCandidateStore.upsert_candidate()`，
  Candidate 的幂等、并发、workspace 隔离和 digest audit chain 继续由 EVO-01.3a 保证。

## 验收证据

- 同一分钟的相同直接反馈幂等，下一分钟形成第二条唯一 Evidence；
- 直接用户与 Agent interpretation 聚合为同一 Candidate，但保留两个 source kind；
- preference/cancel/praise 不创建 Evolution DB；
- 绝对 scope、中文/不稳定 topic 和伪造 source URI 失败关闭；
- 包含模拟 token 的 summary 不出现在 SQLite 文件字节中；
- Agent Tool 无 durable turn 时不写库；真实 `AgentEngine.run_streaming()` 在执行期签发信封、结束后
  清除，并注册 Tool；
- 既有 Harness/Self Review Evidence、Candidate 和 Store 回归保持通过。

## 未完成项

HAR-09.2 才实现跨时间窗、provider/model/platform 的 Aggregator；HAR-09.3 才实施最小次数、
严重度、拒绝冷却和显著新证据 policy；Review Queue 与 HAR-08 before/after outcome tracking 仍未
实现。因此本切片只产生 Draft，不产生 Proposal 或 promoted 状态。
