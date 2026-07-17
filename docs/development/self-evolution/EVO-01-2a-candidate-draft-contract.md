# EVO-01.2a Candidate Draft 契约

## 目标

把一个机械根因的多次 `EvolutionEvidence` 变成稳定、隐私安全、可审查但不可直接执行的 Candidate Draft。该 Draft 是 EVO-01、HAR-09 Review Queue 与未来 UI Candidate 页面之间的共享边界，不调用 `self_modify`，也不宣称已通过 eligibility。

## 契约字段

`EvolutionCandidateDraft` 固定包含：

- `candidate_id`：`evc_` + fingerprint 前 24 位，同根证据增加时保持稳定；
- `fingerprint`：finding code、root fingerprint 与相对 scope 的规范化 SHA-256；
- `finding_code`、`kind` 与相对 `scope`；
- 带来源的 `hypothesis`，当前 builder 只产生 `deterministic_template`；
- 带 `candidate-draft-v1` policy version 的 risk level 与原因；
- 至少一个可机械复核的 expected metric；
- 稳定排序的唯一 Evidence、发生次数、首末观察时间和来源集合；
- 固定 `status=draft`、`experiment_eligible=false`。

Candidate 不能通过自然语言把自己升级为可实验状态。EVO-01.4 必须在 protected scope、可验证性、冷却期和证据强度 gate 全部通过后另行产生资格决定。

## 构建规则

`build_candidate_draft()`：

1. 接受 1—10,000 个 `EvolutionEvidence`；
2. 相同 evidence id 的完全一致重试幂等去重，冲突内容明确拒绝；
3. 只允许 root fingerprint、finding code 与 scope 全部一致的 Evidence 聚合；
4. 按带时区 observation time + evidence id 稳定排序；
5. 根据 finding policy 确定 correctness/maintainability/reliability/safety；
6. 产生版本化风险说明和来源对应的机械指标；
7. 用确定性模板形成 hypothesis，不读取原始对话、stdout 或源码；
8. Candidate id 不包含 observation 数量，因此从 1 次增长到 100 次仍是同一候选。

Harness 工具 Evidence 的 scope 已从不稳定 call/evidence id 改成 `kind:producer:tool` 根标签；同一工具问题跨 run 可以稳定进入同一 Candidate，而原始观察仍由 Evidence URI 区分。

## 隐私与安全

- scope 不能是绝对路径，任何位置出现绝对路径前缀、`..` 或控制字符都会拒绝；
- LLM hypothesis contract 拒绝常见 API key/password/token/Bearer 模式和本机绝对路径；
- Draft 只嵌入已经脱敏的 Evidence，不保存用户原始目标、session text、secret、stdout 或源码；
- 当前来源均为 hard evidence，契约仍机械要求至少一个 hard evidence；
- risk、kind、source kinds、fingerprint、时间窗口和 required metric 都会在反序列化时重新核验，不能伪造 id 或字段组合。

## 验收标准

- 同根 1 次与 100 次 observation 得到相同 candidate id/fingerprint；
- 重复投递同一 Evidence 不增加 occurrence count；
- 不同 root、finding 或 scope 不能误合并；
- 100 个 observation 的首末时间和 occurrence count 与事实一致；
- secret 与工作区绝对前缀不出现在 Candidate JSON；
- hardcoded secret 映射为 safety/high，Self-Review 指标为 finding count 降至 0；
- Draft 始终 `experiment_eligible=false`；
- 伪造 candidate id、fingerprint、risk、时间窗口或来源集合会被模型拒绝；
- Harness 与 Self-Review Evidence 的既有定向测试继续通过。

## 明确未完成

- EVO-01.3 的持久 Store、并发 merge、时间窗和 collision fixture；
- EVO-01.4 eligibility/protected-scope/cooldown gate；
- EVO-01.5 可解释排序与权重审计；
- EVO-01.6/HAR-09 的 list/detail/reject/defer Review Queue；
- runtime metric、Eval 与用户 feedback Evidence adapter；
- LLM hypothesis 生成服务及人工审查流程。
