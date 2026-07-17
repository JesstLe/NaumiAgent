# EVO-01 自我审查证据与改进候选

## 目标

把 self_review、Harness failures、用户反馈、性能回归和安全发现转成去重、可排序、可审查的
Evolution Candidate，而不是直接触发 self_modify。

## 子模块

- EVO-01.1 Evidence adapters：静态扫描、Harness、Eval、feedback、runtime metrics。
- EVO-01.2 Candidate schema：id、fingerprint、scope、hypothesis、risk、evidence、expected metric。
- EVO-01.3 Dedup/merge：同根因聚合，保留发生次数、平台、模型和时间窗口。
- EVO-01.4 Eligibility：证据强度、影响、可验证性、修改边界、冷却期。
- EVO-01.5 Prioritization：severity×frequency×confidence÷cost/risk，字段透明。
- EVO-01.6 Review surface：list/detail/approve experiment/reject/defer。

## 规则

- LLM 建议只能形成 hypothesis；至少一个硬证据才可进入 experiment eligible。
- 用户偏好/产品 taste 与代码缺陷分开，不让模型伪装成机械正确性。
- Candidate 不保存原始用户对话、secret 或完整源码，只引用 Evidence URI/digest。

## 验收标准

- 相同问题跨 100 次 run 聚合为一个 candidate；不同文件/根因不误合并。
- 无硬证据、无法验证或触及 protected scope 的候选不可自动实验。
- 排序每个因子可解释；改变权重有版本和 audit。
- reject/defer 冷却规则有效，不重复打扰用户。
- A3：真实 self_review + Harness failure + runtime metric 形成候选详情。

## 分阶段实现

- EVO-01.1a Harness 失败证据适配器：已实现。耐久 Harness run 经确定性 Explainer 分类后，
  转成只含内部 URI、完整摘要、失败分类和 root fingerprint 的 frozen Evidence；不复制用户目标、
  会话或源码。实现边界与证据见 `EVO-01-1a-harness-failure-evidence.md`。
- EVO-01.1b Self-Review 静态证据适配器：已实现。现有 `self_review` 改用有界 AST 事实扫描，
  secret 值不会进入展示、模型输入或 Evidence；每个 finding 只引用相对 scope 与完整文件摘要。
  实现边界与证据见 `EVO-01-1b-self-review-static-evidence.md`。
- runtime metric/Eval/feedback adapters、Candidate schema、Dedup、Eligibility、Prioritization 与
  Review surface 仍为 planned；不得把 EVO-01 整体标记为完成。
