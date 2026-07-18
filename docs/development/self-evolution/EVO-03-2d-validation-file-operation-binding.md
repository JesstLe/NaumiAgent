# EVO-03.2d Validation File Operation Binding

## 目标

让 Validation Plan 不再只知道“要验证哪个路径”，而是从可信 Mutation Receipt v2 继承该路径是
`modify` 还是 `create`，以及 RED/GREEN 两侧应出现的精确内容摘要。该信息是新建文件 RED fixture、
候选 GREEN source 校验和同 fixture 对照的共同前置。

## Validation Plan v2

Planner 现在默认生成 `schema_version=2`、`policy_version=evolution-validation-plan-v2`。每个
`ValidationFileRequirement` 新增：

- `operation=modify|create`；
- `baseline_sha256`：modify 必填，create 必须为空；
- `candidate_sha256`：modify/create 均必填。

字段直接来自已完成 Generation→Guard→Writer→Mutation Receipt v2 的 `MutationReceiptFile`，并进入 Plan
canonical digest。Planner 不重新读取或猜测工作区内容。

机械约束：

- modify 必须同时有 before/after digest；
- create 必须没有 before digest且必须有 after digest；
- path、operation 或任一 digest 漂移都会使 Plan digest 失效；
- 真实 Plan builder 仍重新验证 Contract、Lease、Snapshot 与完整 Mutation Receipt authority。

## v1 兼容读取

历史 `evolution-validation-plan-v1` 仍可解析：缺失的新字段恢复为 `operation=unknown`，校验摘要时使用原始
v1 payload，不把新增默认字段错误计入旧 digest。

兼容读取不等于执行授权。RED/GREEN executor 必须要求 Plan v2；v1/unknown operation 保持 fail-closed，
需要由原始 Mutation Receipt 重新生成 v2 Plan。

## RED baseline 语义

EVO-03.2c executor 按 v2 operation 读取 baseline：

- modify：Git baseline 必须存在普通 blob，内容 SHA-256 必须等于 `baseline_sha256`；
- create：Git baseline 必须不存在该 path，使用受控空 Python blob 作为 RED fixture，finding count 机械为 0；
- create path 意外存在、modify path 缺失、symlink/submodule 或 before digest 不同全部在 H5a 写入前失败。

空 fixture 只代表“该候选文件在 baseline 尚不存在”，不会读取当前工作区中未跟踪的 candidate 文件，也不会
把任意缺失路径都解释为成功。

## 验收证据

- 真实 Mutation Receipt→Validation Plan 链生成 schema v2，并精确保留 modify before/after digest；
- legacy v1 Plan 仍能用原始 digest 解析，operation 恢复为 unknown；
- 真实 Git create fixture：baseline commit 只有 README，当前工作区有未跟踪 `sample.py`；5 次 RED
  结果均为 count=0，工作区状态不变；
- modify before digest 被替换后，executor 返回 `baseline_blob_digest_mismatch` 且 H5a 无记录；
- 既有 modify、symlink、Profile trust、partial resume 与 H5a 冲突回归继续通过。

## 当前不足与下一步

- Plan v1 只读兼容，不自动升级；升级必须重新取得原始 Receipt authority；
- EVO-03.3a 已重新验证 candidate Lease、HEAD/branch、精确 dirty path 集合和每个 `candidate_sha256`，并
  复用 RED Request 的 metric/seed/order/平台身份生成独立 GREEN H5a cohort；
- 下一切片实现 EVO-03.4a Self-Review Quantitative Comparison Receipt，消费真实 RED/GREEN 样本而非重算。
