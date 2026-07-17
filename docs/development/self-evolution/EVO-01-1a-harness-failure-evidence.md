# EVO-01.1a Harness 失败证据适配器

## 目标

把已落盘的 Harness 运行、检查、回执和工具证据转换为 Evolution 层可消费的机械证据，同时不复制用户目标、会话正文、源码内容或 secret。本切片只建立证据输入，不创建、排序或批准 Evolution Candidate。

## 输入与输出

输入必须是 `HarnessStoredRun`。适配器先复用 `HarnessExplainer` 的确定性失败分类，再回到原始存储事实取得完整 SHA-256；不从 LLM 文本中提取结论。

每条 `EvolutionEvidence` 包含：

- 固定 schema version 和基于观察摘要生成的 `evidence_id`；
- `harness_failure` 来源类型与内部 `harness://`/`chat-run://`/`artifact://` URI；
- 失败分类、观察时间和 `hard_evidence=true`；
- 用于后续聚合的 `root_fingerprint`；
- 一个或多个 URI + 完整 SHA-256 引用。

明确禁止保存 objective、session id、检查输出、warning 原文、changed file 内容和完整源码。

## 确定性与去重边界

- observation identity 包含 run、时间、root fingerprint 和证据引用，因此不同运行不会伪装成同一观察；
- root fingerprint 不包含 run id，而由失败分类、check key + argv 摘要、证据 kind/producer/tool 和 receipt 来源构成；
- 同一检查在多个运行中失败会得到相同 root fingerprint；不同检查命令不会误合并；
- root fingerprint 只是 EVO-01.3 的一个输入，不是最终根因判定。相同检查内部的不同失败原因仍需后续 artifact classifier 区分。

## 安全约束

- 只接受无 query/fragment 的内部 URI scheme，避免 API key 随 URL 参数进入候选库；
- digest 必须是完整 64 位小写 SHA-256，不能用 UI 展示用短摘要冒充完整证据；
- 已验证运行和仍在运行的 run 不产生 failure evidence；
- 找不到具体 check/evidence/receipt 引用时，以脱敏 run status 摘要作为机械引用，不复制正文；
- 返回 Pydantic frozen contract，未知字段和重复 URI 被拒绝。

## 验收标准

- 真实 `HarnessExplainer` 的 failed check 产生 `verification_failure` hard evidence；
- evidence 引用 check 与 receipt 的完整 SHA-256；
- 序列化结果不含 objective、session id 和 changed file；
- 同根检查跨 run 的 root fingerprint 一致、observation id 不同；
- 不同 check argv 的 root fingerprint 不同；
- verified/running run 返回空结果；
- 仅运行 Evolution adapter 与相关 Harness 小模块测试。

## 后续依赖

EVO-01.1b 再接入 self_review 静态扫描与 runtime metric adapter；EVO-01.2 基于多来源 evidence 建立 Candidate schema；EVO-01.3 才实现时间窗聚合、冲突拆分和 100-run 去重验收。本切片不会触发 `self_modify`，也不会绕过用户批准或 protected scope。
