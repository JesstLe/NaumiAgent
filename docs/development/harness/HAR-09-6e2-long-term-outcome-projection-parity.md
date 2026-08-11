# HAR-09.6e2 Long-Term Outcome Projection Parity

## 目标

让 `HAR-09.6e1` 的 current Long-Term Outcome head 通过同一 typed Proposal Outcome projection 到达 Workbench、
New UI 和 Textual TUI fallback。展示层不得自行查询 supersede 表、猜测“最新”记录或把 passing Assessment 直接
渲染成 promoted；所有 head、lineage 与动态 authority 均由 Python Service 判定。

## Projection v2

`EvolutionProposalOutcomeProjection` 升级为 schema 2 / policy
`evolution-proposal-outcome-projection-v2`，显式区分：

- `outcome_id/sha256`：当前 projection head；没有长期 revision 时等于 rollback root，有长期 revision 时等于
  `evpostlongout_*`；
- `root_rollback_outcome_id/sha256`：immutable `rolled_back` 历史事实；
- `status`：`rolled_back | rollback_recovery_observed`；
- `rollback_outcome_authority`：root 当前 authority；
- `long_term_outcome` 与 `long_term_supersede_event`：必须同时存在或同时为空；
- `long_term_metrics_recorded`：严格等价于 Long-Term pair 存在；
- `long_term_outcome_authority`、`current_long_term_health_authority`、
  `projection_head_authority`：展示动态撤权原因；
- `authority_valid`：当前 head 的最终 authority，不是历史 root 的别名。

Before/After Evidence、Runtime Verification 与 Behavioral Matrix 仍绑定
`root_rollback_outcome_id/sha256`。Long-Term Outcome 和 supersede event 则绑定 current head，并逐字段复验 workspace、
request、root、revision/sequence、prior、recorded time、Workbench Proposal 与 Candidate。这样既不会为适配 UI 改写
旧 evidence，也不会把 current head 错当成旧 root。

## Source 读取与失败关闭

Projection Service 对每个 rollback root：

1. 从 Long-Term Store 读取 request head；
2. 通过 head 的 Assessment ID 读取 exact Outcome/Event pair；
3. head 与 pair 不一致则返回 `proposal_outcome_long_term_mismatch`；
4. 调用 Long-Term Service inspect，重新验证完整 supersede chain、root、Contract、Assessment、heartbeat health 与 head；
5. 把动态 View 组合进 Projection v2。

Long-Term Store、Service 和 root Store 必须是同一对象图；跨 session DB 拼接在构造阶段拒绝。Artifact row 无法读取时
projection source unavailable；artifact 可读取但 heartbeat stale、历史 event 缺失或不再是 head 时仍展示历史
`rollback_recovery_observed`，但 `authority_valid=false`，Workbench 外层状态为 `evidence_invalid`。

## Workbench 与终态门禁

Workbench Service 不再复制一套手写字段判断，而是重新执行完整 Pydantic Projection v2 validation，再核对 session/proposal
外层绑定。`outcome_status` 在 authority 有效时采用 typed `status`，否则固定为 `evidence_invalid`。

Experiment Contract issuer 同时把 `rolled_back` 与 `rollback_recovery_observed` 视为 terminal Outcome；即使长期观察恢复，
也不能为同一 Proposal 再次签发 Contract。治理 Proposal 仍保留 approved 审计状态，`contract_issue_allowed=false`。

## New UI 协议与展示

New UI protocol：

- 接收 Projection v2，并在过渡期只读兼容无长期字段的 v1 rollback projection；
- 严格校验 Long-Term Outcome/Event schema、policy、ID pattern、常量 authority、pair 和 root/current lineage；
- 外层 `outcome_status` 新增 `rollback_recovery_observed`，有效时必须等于 projection status，失效时必须为
  `evidence_invalid`；
- tampered root、event、current head 或 authority component 组合均拒绝整份 snapshot。

New UI Reviews detail 使用绿色 `recovery observed` 语义色，并展示：

- current Long-Term Outcome 与 immutable Rollback Root；
- revision、Assessment 与 ledger head；
- runtime subject/binding；
- observation seconds / operational samples；
- supersede event 与 rollback fact preserved；
- health/head/outcome 三项动态 authority。

## Textual TUI parity

Textual fallback 从同一 Workbench snapshot 渲染相同字段和边界：长期指标显示“已记录”，单独展示长期恢复观察段，
并持续声明不能再次签发 Experiment Contract、标记 promoted 或进入 policy learning。TUI 不读取 SQLite，也不重算 head。

## 验收标准

- [x] 没有 Long-Term head 时 Projection v2 保持 `rolled_back` root 语义；
- [x] current passing head 投影为 `rollback_recovery_observed`，current ID 与 root ID 分离；
- [x] Long-Term Outcome/Event exact pair、root、prior、revision/sequence 和时间逐字段绑定；
- [x] heartbeat stale 后历史 recovery Outcome 仍可见，但 authority 与外层状态动态失效；
- [x] Long-Term Store/Service/root Store 跨依赖组合构造失败；
- [x] Workbench 重新执行 typed v2 validation，不信任任意 reader dict；
- [x] recovery Outcome 继续阻断同 Proposal Contract 签发；
- [x] New UI protocol 校验 v2，同时只读兼容 v1 root projection；
- [x] New UI 展示长期窗口、supersede 和动态 authority，并使用 recovery 语义色；
- [x] Textual TUI 展示同源字段和相同终态边界；
- [x] Python/Node 小模块测试、ruff、compile、registry 和 diff check 通过；未运行全量测试。

## 自我审视与下一步

本切片完成的是 rollback recovery observation 的三端投影，不是成功 rollout 的 promoted Outcome。它没有授予 learning、
promotion 或 execution authority，也没有解决配置/数据 rollback。New UI 当前展示单个 current head 和关键 supersede event，
完整历史链浏览器仍待独立设计，不能把详情卡等同于审计导出。

成功 rollout 路径的第一步已由
[EVO-05.7b1](../self-evolution/EVO-05-7b1-stable-promotion-observation-contract.md) 完成：它冻结 approval、active stable
runtime、Proposal binding 与 finalization 后长期观察规则，但不冒充独立长期效果。
[EVO-05.7b2](../self-evolution/EVO-05-7b2-stable-promotion-runtime-observation-admission.md) 已完成单
installation 的 exact member/runtime 绑定。下一步必须先安全汇集跨安装 Admission，再聚合长期窗口，最后才设计
promoted/superseded ledger；不得复用本 recovery Outcome 越权推广。
