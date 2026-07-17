# HAR-09 Feedback Candidate 与受控提升

## 目标

把重复失败和用户纠正聚合为可审查 Proposal，而不是直接修改 Prompt、规则或代码。

## 子模块

- HAR-09.1 Fingerprint：failure class、module、tool、normalized context 的隐私安全指纹。
- HAR-09.2 Aggregator：次数、最近发生、影响范围、趋势和代表 Evidence。
- HAR-09.3 Candidate policy：最小次数、严重度、冷却期、排除一次性噪声。
- HAR-09.4 Proposal generator：knowledge/profile/prompt/tool/test/code 六类建议。
- HAR-09.5 Review queue：approve/reject/defer/merge，必须有人类或治理策略决定。
- HAR-09.6 Outcome tracking：Proposal 实施后用 HAR-08 比较，记录改善或回退。

## 安全与隐私

- fingerprint 不包含原始用户文本、secret、路径绝对前缀或 stdout。
- 用户拒绝/取消不自动视为 Agent 缺陷。
- 同一根因跨模型重复时合并，但保留 provider/model 维度用于分析。
- Proposal 只写候选表和 Workbench review，不自动触碰仓库。

## 验收标准

- 相同规范化失败聚合，语义不同失败不碰撞；测试固定 collision fixtures。
- 少于阈值不生成候选，高严重度安全问题可走单次升级规则。
- reject 后冷却期内不重复骚扰；新证据显著变化可重新开启。
- Proposal 实施后必须关联 Eval before/after；无改善则不能标为 promoted。
- A3：注入重复失败、用户纠正和噪声，最终队列数量与规则一致。

## 已具备的跨模块前置

- EVO-01.2a 已提供稳定、脱敏且固定不可执行的 Candidate Draft 契约，可作为未来 HAR-09
  Proposal/Review Queue 的输入边界；HAR-09 仍未实现 feedback adapter、聚合策略、Store、
  review action 或 outcome tracking，因此整体继续保持 planned。
