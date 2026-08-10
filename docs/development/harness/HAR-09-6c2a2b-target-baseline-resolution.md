# HAR-09.6c2a2b Target Baseline Resolution

## 状态

已实现。

## 依赖链

```text
HAR-09.6c2a1 Coverage Contract
  -> HAR-09.6c2a2a exact Worker/target Placement
  -> ARC-07.5d1a public Channel Trust loader
  -> current signed Release Channel Catalog
  -> target-specific trusted Build Attestation
  -> source-equivalent Target Baseline Resolution
```

调用方只提供 Rollback Request、原 Comparison 与受信 channel；lane、Worker incarnation、target 和本机 baseline
identity 全部从 durable authority 读取，不能自报 target、version、commit、tree、archive 或 signer。

## Durable Artifact

`EvolutionPostRollbackTargetBaseline` 冻结：

- Coverage/Outcome/Request/Proposal lineage；
- Placement ID/SHA、Worker ID/instance/epoch 与 exact release target；
- 本机 baseline slot、version、target、source commit/tree；
- current Channel Resolution、Catalog、双 Trust Policy 和 target Build Attestation；
- source equivalence / target attestation verified flags；
- content-addressed Resolution ID/SHA 与时间。

Store 与 Placement 共用 evolution session DB，在同一 `BEGIN IMMEDIATE` 事务中要求 exact Placement SHA 已持久化；同一
Placement 只能绑定一个 channel resolution，重复调用幂等，不允许静默换 channel。

## 动态撤权

读取时重新执行 Placement inspect 与 Catalog resolve，并忽略会自然变化的 `resolved_at`，精确比较 Catalog、双 trust
policy、entry、attestation 和 archive origins。任一 authority 变化都令 `baseline_resolution_authority=false` 且
`download_input_authority=false`。

## 用户回执

回执显示 Resolution、Placement、Worker epoch、channel/target、version、source commit/tree、Build Attestation 和动态
authority，并明确标注“未下载、未安装、未下发、无执行/结果/学习/推广权限”。

## 后续切片

1. `HAR-09.6c2a3a` 已先把 exact incarnation heartbeat/accepting/active jobs 纳入 Registry v5 durable authority；
2. `HAR-09.6c2a3b Remote Dispatch`：同时消费 fresh health 与 atomic capacity reservation；
3. `HAR-09.6c2a3e Execution Authorization` 与 `6c2a3f Signed Result Ingestion` 已完成；
4. `HAR-09.6c2b1/6c2b2` Matrix Core 与 typed 双端详情、`HAR-09.6d1` 长期观察契约均已完成；
   下一步为 6d2 managed runtime admission。
