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
  `EVO-01-3a-candidate-store-core.md`。其生产实例现由 ARC-01.4b2b Composition Root 显式装配，
  Feedback/Evolution Review 共享同一 Store，不再由 Service 或 Engine 隐式创建。
- EVO-01.4a Eligibility Policy v2：已实现。机械证据、重复直接反馈、Agent-only 信号、机械
  verifier 和 protected scope 由版本化纯函数给出稳定 reason codes；只判断是否可进入人工审阅，
  冷却期和 experiment contract 未完成前永不授予实验资格。详见
  `EVO-01-4a-candidate-eligibility-policy.md`。
- [EVO-01.4b](EVO-01-4b-composite-source-authority-router.md) Composite Source Authority
  Router：已实现。动态 Evidence 类型必须在 composition root 完整注册；多 reader 并发重验、异常和
  非 bool 结果失败关闭，Review reader 不再允许被后绑定者静默覆盖。
- [EVO-01.1c / EVO-06.1c1](EVO-06-1c1-h5c-quantitative-regression-opportunity.md) H5c 定量回归
  Evidence adapter：已实现。重读两组 H5a cohort 并完整重建 H5c，只把 primary typed metric 的
  95% CI 确认回归写入 Evidence v2；Candidate 保留原 metric/direction/target 并在 Review 前动态重验。
- [EVO-06.1c2](EVO-06-1c2-goal-backed-explicit-need-opportunity.md) Goal 明确需求 adapter：已实现。
  只接受用户显式创建且未终结的 durable Goal，Candidate 不保存 objective/note/session 正文；完成、
  取消或篡改会动态撤权，独立 acceptance runner 缺失时禁止自动实验。
- [EVO-06.1c3](EVO-06-1c3-durable-tool-catalog-miss-opportunity.md) exact Tool Catalog miss
  adapter：已实现。只持久化安全 `select:<tool-name>` 缺失，绑定完整目录摘要；目录变化、目标工具出现、
  篡改或跨工作区会撤权，自然语言查询不进入 Candidate。
- [EVO-06.1c4](EVO-06-1c4-cross-source-opportunity-prioritization.md) 跨来源 Opportunity Portfolio：
  已实现。最多 500 条权威 Candidate 在固定 30 天窗口内完成 authority/冷却 Gate、同 lane 同日去重、
  domain 聚类、影响计数和透明评分；先排序再应用显示 limit，Agent-only、调用/token 数和簇大小不能刷榜。
- HAR-09.1a Feedback adapter：已实现。直接用户反馈和 Agent 对 durable user turn 的解释使用
  不同 source kind，摘要不落库，非缺陷反馈不生成 Candidate；所有结果仍固定不可执行。
- EVO-01.6a Candidate 只读审阅面：已实现。用户通过 `/evolution list/priorities/detail`、Agent 通过
  `evolution_candidates` 读取同一服务；过滤、详情、审计链和资源上限均为确定性实现，读取不改变
  Candidate。设计与证据见 `EVO-01-6a-readonly-review-surface.md`。
- EVO-01.6a1 Typed Review UI：已实现。默认新 UI 通过 ARC-03 event registry 管理的 typed snapshot
  展示全屏列表/详情与 Eligibility Gates；TUI 使用同一 Service 的线性降级。详见
  `EVO-01-6a1-typed-review-ui.md`。
- HAR-09.2a Candidate Aggregation View：已实现单 Candidate 的稳定时间窗、趋势、维度分布与代表
  Evidence，并进入共享 Review detail；详见 `../harness/HAR-09-2a-candidate-aggregation-view.md`。
- HAR-09.5a/5b1/5b2a 已实现 Proposal 显式入队、持久来源、治理状态机、有效冷却与 Eligibility
  只读接线；单条噪声不能越过 reject/defer，显著新证据规则有版本和审计，New UI/TUI 可见同一
  治理结论。Workbench 决策交互页仍未完成。
- runtime 在线指标 adapter、自然语言缺失意图 adapter、可验证语义同根聚类、
  完整 experiment Eligibility 和 approve/reject/defer 页面动作仍为 planned；不得把
  EVO-01 整体标记为完成。
