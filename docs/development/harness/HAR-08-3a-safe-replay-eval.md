# HAR-08.3a Safe Replay Eval Runner

## 目标与范围

本切片把已经交付的 HAR-05 安全 Replay 接入 HAR-08 的 typed Result、Identity、Comparator 与
Baseline 基础设施。它评测的是“持久运行证据能否按同一规则完整复现”，不是重新执行任务。

入口统一为：

- 用户：`/harness eval replay [run-id|latest]`；
- Agent：只读、并发安全的 `harness_eval_replay` Tool；
- 底层：`HarnessService.eval_replay_run()`；
- 结果：一个 `safe_replay@1` runner、一个 `replay_integrity` case。

本切片不包含真实 Tool/Check 执行、模型调用、Sandbox 或 Live Eval，也不新增存储表。

## 不可破坏的安全性质

1. Eval 路径只读取当前工作区的 Harness run 与既有 Replay baseline。
2. 如果 run 尚无 baseline，Eval 返回 `replay_unavailable`，不会为了得到结果而创建 baseline。
3. 不调用模型、Tool、Check、session 或 completion lifecycle。
4. Result 不嵌入 timeline、artifact 内容、原始输出、changed path 或 secret，只保留状态和摘要绑定。
5. 运行前后各捕获一次 Git source identity；期间源码变化时不生成可用 Baseline identity。
6. 工作区查询继续由 HarnessStore 的 workspace scope 隔离，外部 run id 按未找到处理。

因此 `no_model` 与 `no_side_effect` 是 runner 的强制 guardrail。Policy 层缺少任一证据时必须返回
inconclusive，不能形成通过结论。

## 确定性身份

Suite id 由 `sha256(run_id)` 的前 16 位生成，避免不同 run 共用同一 Baseline selector。Suite digest
绑定以下字段：

- runner version：`safe_replay@1`；
- run id；
- baseline manifest SHA-256；
- Replay rule version；
- baseline explanation SHA-256；
- 预期状态 `reproduced`。

标准 HAR-08 configuration identity 继续绑定 Profile digest、Policy digest、source identity、平台与
Naumi 版本。Replay runner 不使用模型，因此 `model=null`。Profile 缺失/无效、源码不可读或运行中
变化时，Result 仍可说明 case 事实，但不得获得可晋升身份。

## 状态与错误分类

| HAR-05 Replay | HAR-08 Case | 稳定 code | 含义 |
| --- | --- | --- | --- |
| `reproduced` | `passed` | 空 | 证据、规则和摘要复现 |
| `changed` | `implementation_failure` | `replay_behavior_changed` | 规则或实现行为发生回归 |
| `partial` | `evaluation_error` | `replay_evidence_partial` | 证据不完整，不能评价产品 |
| `corrupt` | `evaluation_error` | `replay_evidence_corrupt` | baseline/artifact 完整性失败 |
| 无既有 baseline/store 不可用 | `evaluation_error` | `replay_unavailable` | Eval 前置证据不可用 |
| run 不存在 | `evaluation_error` | `replay_not_found` | 当前工作区无匹配运行 |

“partial/corrupt/unavailable”绝不能计入 implementation failure，否则会把评测设施问题误报成产品回归。

## 用户闭环

推荐流程：

1. 对旧运行先显式执行 `/harness replay <run-id>`，由用户可见路径建立 legacy baseline；新完成运行
   由 HarnessStore 在 completion transaction 中持久化 baseline。
2. 执行 `/harness eval replay <run-id>` 获得 typed Eval 结果。
3. 需要重复样本、晋升或 compare 时，后续切片把该 runner 接入现有 H5 batch 流程；本切片不进行
   隐式写入或自动晋升。

## 验收证据

- 四种 Replay 状态逐一验证 Eval 分类、runner、主指标与双 guardrail。
- 验证 Result JSON 不包含 timeline 或 artifacts。
- 验证源码前后身份变化会阻止 Baseline identity。
- 验证缺少 baseline 时 Eval 返回错误且数据库仍无 baseline 行。
- 验证已完成 run 的持久 baseline 可重复读取，Eval 不改变 baseline。
- 验证 Agent Tool 的 schema、只读/并发 metadata 与错误输入。
- 验证共享 Slash 路由与 Agent Tool 显示相同的 typed 语义。
- 只运行相关 Ruff、Replay Eval、Tool、Surface、Policy 与文档治理测试，不运行全量测试。

## 后续依赖与限制

- H5 repeated batch/promotion/compare 尚未接受 `safe_replay@1` 作为可选 runner；本切片只提供单次
  typed Result 与 comparator-compatible identity。
- HAR-08.4 Sandbox Runner 硬依赖 ARC-04 隔离 worker。现有 subprocess timeout/process-group
  以及 Docker 不可用时的本机 fallback 都不足以证明 host filesystem/network 隔离。
- HAR-08.5 Live Runner 仍需 provider/model capability、reasoning、成本、token、deadline 与显式
  `--live` 权限合同。
- 三平台一致性尚需 CI fixture 验证；因此 HAR-08 整体状态保持 partial。
