# HAR-08.5b 声明式 Live Eval Suite 与重复批次

## 状态

已实现。本切片在 HAR-08.5a 单次传输挑战之上增加受 Profile 声明约束的 Live Suite、5..20 次重复
样本、同一 cohort identity、逐样本 H5a 持久化，以及批次级时限和成本回执。HAR-08 仍为
`partial`；Provider 远端取消/计费证明、专用 typed 进度页和三平台 Provider matrix 不在本切片内。

## 用户入口

```text
/harness eval live <suite-id|declared-path> --repeat 5..20 \
  [--batch <id>] [--model <id>] [--timeout 5..3600] [--max-cost 0..10]
```

Agent Tool 为 `harness_eval_live_batch`。Slash、New UI、Textual TUI 和 Agent Tool 最终调用
`HarnessService.eval_live_batch()`；normal 只确认一次，bypass 按全权限语义直通。该操作会调用外部模型并
写入 H5a，因此不是只读 Tool。

## 声明式 Suite

Profile 通过 `evals.live_suites` 声明工作区内 YAML。解析器拒绝工作区逃逸、未声明路径、歧义 id、非 UTF-8、
未知字段和超过 256 KiB 的文件。当前 schema 只允许固定的 `live_transport_echo` runner：

- Suite 最多 10 个 case；
- 每个 case 固定 `naumi_live_echo@1` Prompt 和 exact-match 机械期望；
- case 显式声明时长、成本和输出 token 上限；
- sample 预算必须覆盖全部 case 预算；
- 不接收任意 Prompt、用户内容、工作区内容、Tool 或 LLM Judge。

默认仓库 Suite 位于 `docs/harness/evals/live-transport-core.yaml`，并由 `.naumi/harness.yaml` 明确声明。

## 批次请求与执行边界

`HarnessLiveBatchRequest` 是严格、冻结、防篡改合同，绑定 canonical UTC 创建时间、Suite 原始摘要、Profile
摘要、请求模型、5..20 次重复、批次时限/成本和 runner 版本。每个 case 的持久 Live evidence 都保存
`batch_request_sha256`，从 H5a 样本可以机械回溯到同一批次权威。

执行遵循以下规则：

1. 未受信任的 Profile 在零 Provider 调用下失败关闭；随后捕获 Git source identity 与
   capability/reasoning model identity。
2. 每个 sample 复用 5a 的真实传输 runner；每个 case 仍独立执行调用前成本预检。
3. Provider/timeout/合同错误出现后不重试；同 sample 的后续 case 标记为未调用并停止后续 sample。
4. 每次 Provider 回执累计真实 token 和成本。实际成本越界后立即停止；已发生的远端成本不能回滚，回执以
   `actual_cost_exceeded=true` 明示。
5. 批次结束后复验源码和模型合同；Provider 实际模型缺失或样本间漂移时不生成可晋升 identity。
6. 只有同 source/config/model/provider identity 下完成全部样本，才保留 Baseline eligibility。

## H5a 持久化

Service 按 sample index 顺序调用现有 `HarnessStore.record_eval_result()`。Store 的
workspace/batch/suite/sample 不可变键、内容摘要、幂等重试和冲突拒绝规则保持不变。写入中途失败时只保留已
确认的连续前缀，并返回 `live_batch_persistence_failed` 或 `live_batch_persistence_incomplete`，不会覆盖既有
事实，也不会自动创建或切换 H5b Baseline。

每个 case 保存：Provider 实际模型、finish reason、输入/输出/总 token、实际成本、响应 SHA-256、5a transport
receipt SHA-256、batch request SHA-256 和 exact-match 结论。原始挑战、模型输出、reasoning、用户内容与
Provider 私有异常均不持久化。

## 状态与失败语义

批次回执绑定 requested/completed/persisted、调用数、token、成本、identity 和已保存样本摘要：

- `completed`：全部样本执行并保存，且未超出总成本；
- `partial`：预算耗尽、评测基础设施失败、样本不完整或只保存连续前缀；
- `error`：持久化权威失败；已完成执行证据仍保留；
- `actual_cost_exceeded`：Provider 回执成本超出请求上限，停止后续调用；即使随后持久化失败，独立布尔字段仍
  保留超支事实。

## 权威代码

- Suite/Request/Runner/Batch receipt：`src/naumi_agent/harness/eval_live_suite.py`
- 单次 transport：`src/naumi_agent/harness/eval_live.py`
- typed H5a evidence：`src/naumi_agent/harness/eval_models.py`
- Service/Tool/Slash/权限：`src/naumi_agent/harness/service.py`、`tools.py`、`main.py`、
  `safety/permissions.py`
- 聚焦测试：`tests/unit/test_harness_eval_live_suite.py`

## 验收标准

- Profile 外、工作区外、过大或 schema 非法 Suite 在零 Provider 调用下拒绝。
- 5 个成功样本形成一个含实际 Provider 模型的 identity，且每个证据绑定同一 batch request digest。
- 不可信价格合同、Provider 漂移、实际超支和持久化失败均有稳定且互不混淆的状态。
- 一个 case 出现基础设施错误后，同 sample 后续 case 和后续 sample 均不发送请求。
- H5a 只保存不可变连续前缀；normal 确认、bypass 直通；Slash 和 Tool 共用 Service。
- 只运行相关 Harness/权限/路由聚焦测试，不以全量测试替代证据。

## 已知限制与下一切片

1. 本地取消无法证明 Provider 已停止远端推理或计费；需要 Provider cancellation/billing 对账合同。
2. 当前专用 Suite 只覆盖固定传输协议，不代表工具、长上下文、视觉、结构化输出或复杂推理质量。
3. 新 UI/TUI 可通过共享 Slash 查看最终回执，但尚无 Live 专用 typed progress/history 页面。
4. 尚无 macOS/Linux/Windows 的真实 Provider matrix；当前不能声称跨平台 Provider 完整可用。
5. H5b/H5c 已可消费合格 cohort，但本切片不自动晋升或比较。

HAR-08.5c1 已补齐 Live batch 类型化进度和 New UI/TUI 同源展示，详见
`HAR-08-5c1-live-eval-typed-progress.md`。下一步 8.5c2 应建立 Provider cancellation/billing evidence
contract，再建立三平台、主流 Provider 的小额真实验证矩阵。
