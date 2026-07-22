# EVO-03.7b1 最终 Evaluation 聚合合同

## 目标

EVO-03.7a 的单 lane receipt 被类型系统固定为非最终证据。最终聚合在读取这些 receipt 前，必须先知道
哪些平台和 RED/GREEN lane 是完整集合，否则“当前收到了哪些”会被错误当成“本来要求哪些”。

本切片从防篡改 `EvolutionAdversarialBatchRequest` 生成一个确定性、不可执行的 Aggregation Contract，
冻结最终 Evaluation Receipt 的覆盖边界。

## Authority

合同完整嵌入并重新验证 Batch Request，同时投影：

- workspace、request id/digest；
- Validation Plan id/digest；
- Candidate id/revision；
- suite、每 lane 样本数；
- 精确的 required platforms；
- 每个平台对应的 RED/GREEN lane order 与 batch id；
- 必须额外提供一个 Interventional lane。

合同自身使用 canonical JSON + SHA-256 形成 `evagg_*` 稳定 identity。任何删平台、换 batch、修改样本数、
替换嵌入 request 或修改 Candidate/Plan 投影都会验证失败。

签发时还会用只读 Git object 命令验证 workspace 是精确仓库根、baseline commit 存在，并重新计算该 commit
完整 `ls-tree` 摘要。当前 HEAD 可以在评测后前进，但来自其他仓库或不同 source tree 的 request 不能被重新
绑定到当前 workspace。

## Durable Store

Aggregation Contract 写入现有 Evolution/Session SQLite 路径中的独立表：

- `contract_id` 为不可变主键；
- 相同合同幂等返回；
- 并发重复签发由 SQLite `BEGIN IMMEDIATE` 串行化并返回同一 artifact；
- 相同 identity 的不同内容拒绝覆盖；
- 读取时同时核对 row columns、typed payload 和合同摘要；
- Store 损坏以稳定错误码失败关闭。

该 Store 只保存有界 typed request，不保存源码、用户对话、凭据或执行输出。

## 双通道

- 用户：`/evolution evaluation-contract <workspace-relative-request.json>`。
- Agent：`evolution_evaluation_contract(batch_request={...})`。

用户路径必须位于当前 workspace 内、解析符号链接后仍不可逃逸，且必须是最多 1 MiB 的普通 JSON 文件。
两条通道复用同一个 Issuer、Builder、Store 和 renderer。

## 真实验证

聚焦测试从真实 Harness Profile、Trust、Validation Plan 与 Adversarial Probe Contract 构建 Batch Request，
并验证：

- matrix probe 机械冻结 Linux/macOS/Windows 六条执行 lane；
- Aggregation Contract 精确生成三组 RED→GREEN pair；
- 重复签发和 Store 重开保持同一 artifact；
- 删除平台后即使保留旧 digest也不能通过 typed validation；
- 非 Git workspace、缺失 baseline commit 或 tree 摘要不一致时拒绝签发；
- Agent Tool 与 slash 输出相同合同；
- `../` 与解析后 workspace 外路径拒绝；
- SQLite column 篡改后读取失败关闭。

## 明确未完成

- 本合同不是最终 Evaluation Receipt，字段强制保持
  `candidate_evaluation_complete=false`、`final_receipt_issued=false`。
- EVO-03.7b2 仍需从 Store 重读一个 Interventional lane 和合同声明的全部 Adversarial lane，重新验证
  platform/batch/sample/Plan/Candidate/digest 后签发最终 receipt。
- 跨平台 dispatcher 尚未交付；三平台合同只能表达完整要求，不能证明 Windows/Linux worker 已执行。
- EVO-04 不得消费本合同直接作 accept/reject；只能消费后续最终 receipt。
