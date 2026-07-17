# HAR-08.6b Static Eval Baseline Identity 闭环

## 1. 目标

把 HAR-08.6a 的身份契约接入已经交付的离线协议 Eval，使 `/harness eval`、Agent
`harness_eval` Tool 和 `HarnessService.eval_suites()` 返回同一个可比较身份，而不是只有内部
数据类。该闭环保持完全离线、无模型、无副作用，不写 Eval/Baseline 数据库。

## 2. 执行时序

1. Service 读取当前 Profile snapshot，同时传入真实 `profile_digest` 与 trust 状态；
2. 评测线程在运行所有选中 Suite 前采集一次 Git source identity；
3. Suite loader 在同一次有界读取中计算原始 YAML SHA-256；
4. 运行现有生产协议 runner，不调用模型、命令或 Store；
5. 全部 Suite 结束后再次采集 Git source identity；
6. 前后身份一致时，为每个 Suite 生成 `protocol_hello@1`、repetitions=1、live=false、
   `model=null` 的 Baseline Identity；
7. 前后身份不一致或 Git 不可验证时保留 Eval 结果，只将 Baseline 标记为不可用。

多 Suite 共享同一对 Git snapshot，避免每个 Suite 重复扫描整个工作区，并保证一次 Report 内的
源码比较边界一致。

## 3. Result 契约

`HarnessEvalSuiteResult` 新增：

- `suite_sha256`：实际读取的 Suite 原始字节摘要；
- `baseline_identity`：成功生成时的完整 typed identity；
- `baseline_identity_code`：身份不可用时的稳定机器码。

`canonical_payload()` 包含上述稳定字段，仍排除 duration。Comparator 后续必须读取 typed
identity，不能从 Markdown 文案反向解析。

## 4. Baseline 与 Eval 错误隔离

| Baseline code | 含义 | Eval 结果 |
| --- | --- | --- |
| `baseline_source_unavailable` | 工作区不是可验证 Git 仓库或 Git 不可读取 | 保留 |
| `baseline_source_changed` | 评测期间 HEAD/index/worktree 状态变化 | 保留 |
| `baseline_suite_unavailable` | Suite 加载失败，无法形成有效摘要 | 按原分类返回 |
| `baseline_configuration_invalid` | Profile/Suite/Runner 身份字段无效 | 保留 |

Baseline 身份错误不伪装成被测实现失败，也不把已经完成的 case 丢弃。

## 5. 用户表面

共享 Markdown renderer 在每个 Suite 标题下显示：

- 短 `identity_sha256` 与“可晋升/不可晋升”；
- 最重要的一条治理提示；
- 无法生成时显示稳定 code 对应的中文原因。

Slash 与 Agent Tool 均由 Service 返回同一 typed report 后调用该 renderer，因此新 UI 与 TUI
不需要维护第二套 Baseline 判断。

## 6. 已验证场景

- 干净真实 Git repo 生成 `model=null`、可信 Profile、可晋升身份；
- Suite digest 与磁盘原始字节一致，并进入 configuration identity；
- 未信任 Profile 仍可离线评测，但 UI 明确显示不可晋升；
- 非 Git 工作区 Eval 通过、Baseline 显示不可用；
- 在 runner 返回后注入工作区变化，前后 fingerprint 不同并拒绝 stale identity；
- Service、Slash 与 Agent Tool 的 canonical result 和显示保持同源；
- 原有 Eval schema、budget、fixture integrity 和错误分类回归通过。

## 7. 未完成

- 尚未把 identity/result 写入 H5 Store；
- 尚未实现 Baseline promote、list、delete 与 Comparator；
- 尚未为 Replay/Sandbox/Live runner 声明各自 runner version 和身份维度；
- 尚未在全屏 New UI 中提供独立 Baseline 详情页，本阶段通过共享 Markdown 正确显示。
