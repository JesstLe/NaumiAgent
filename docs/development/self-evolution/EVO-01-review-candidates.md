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
- EVO-01.2a Candidate Draft 契约：已实现。同根 Evidence 可幂等构建稳定、脱敏、带风险和
  机械指标的候选草案；所有 Draft 固定不可执行，不能绕过后续 eligibility。设计与证据见
  `EVO-01-2a-candidate-draft-contract.md`。
- EVO-01.3a Candidate Store Core：已实现。平台原生用户状态库以 versioned SQLite 保存
  不可变 Evidence、Candidate materialization 和 digest audit chain；并发投递、100 次同根
  聚合、幂等重试、工作区隔离与篡改检测均有机械测试。设计与证据见
  `EVO-01-3a-candidate-store-core.md`。
- EVO-01.4a Eligibility Policy v1：已实现。机械证据、重复直接反馈、Agent-only 信号、机械
  verifier 和 protected scope 由版本化纯函数给出稳定 reason codes；只判断是否可进入人工审阅，
  冷却期和 experiment contract 未完成前永不授予实验资格。详见
  `EVO-01-4a-candidate-eligibility-policy.md`。
- HAR-09.1a Feedback adapter：已实现。直接用户反馈和 Agent 对 durable user turn 的解释使用
  不同 source kind，摘要不落库，非缺陷反馈不生成 Candidate；所有结果仍固定不可执行。
- EVO-01.6a Candidate 只读审阅面：已实现。用户通过 `/evolution list/detail`、Agent 通过
  `evolution_candidates` 读取同一服务；过滤、详情、审计链和资源上限均为确定性实现，读取不改变
  Candidate。设计与证据见 `EVO-01-6a-readonly-review-surface.md`。
- runtime metric/Eval adapters、provider/model/platform 时间窗聚合策略、
  完整 experiment Eligibility、Prioritization、专用全屏页和 approve/reject/defer 动作仍为 planned；不得把
  EVO-01 整体标记为完成。
