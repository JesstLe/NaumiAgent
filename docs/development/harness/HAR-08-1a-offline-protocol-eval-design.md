# HAR-08.1a 离线协议 Eval 纵向切片设计

## 范围

本切片把 HAR-08.1、HAR-08.2 与 HAR-08.8 的最小必要部分组合成一个可直接使用的能力：
从工作区 Harness Profile 读取已声明的离线协议 Eval Suite，以真实 Python UI 协议实现执行，
并通过 `/harness eval` 和 `harness_eval` Agent Tool 返回同一份确定性结果。

它不是通用 benchmark 平台。本切片不执行模型、命令、网络或浏览器，不写 baseline/store，
不做 Replay/Sandbox/Live runner，不比较多个 commit，也不引入 LLM Judge。

## 为什么现在做

- ARC-03.4a 已提供真实 hello 版本/能力协商，具备可机械评测的稳定边界；
- HAR-08 是 EVO-03 的唯一评测裁判，继续 UI 或 ARC 表层都不能替代它；
- UI-17 仍等待 UI-11..16，CC-02 仍等待 UI-15，当前不能形成完整交付；
- 离线协议 Eval 可在三平台无 provider、无 API key、无副作用运行，是 HAR-08 风险最低但有真实用户价值的入口。

## 权威边界

| 事实 | 权威 |
| --- | --- |
| Suite/Case 定义 | 工作区内、Profile `evals.suites` 显式声明的 YAML |
| hello 解析与协商 | `naumi_agent.ui.protocol` 生产实现 |
| 运行入口与路径约束 | `HarnessService` |
| 结果分类与渲染 | `naumi_agent.harness.eval` |
| CLI/TUI/New UI | 共享 `execute_slash_command()`，不得复制 runner |
| Agent Tool | 调用同一 `HarnessService.eval_suites()` |

Runner 不能复制协议判断逻辑；fixture 的 expected 只负责断言，不成为运行时实现。

## Suite schema

```yaml
schema_version: 1
id: protocol-hello-core
title: UI Bridge hello 协商核心回归
cases:
  - id: modern-compatible
    runner: protocol_hello
    input:
      transport: jsonl
    fixture:
      path: fixtures/hello-modern-compatible.json
      sha256: <64 hex>
    expected:
      outcome: accepted
      selected_version: 1
      capabilities: [heartbeat, typed_ui_messages]
    metrics:
      primary: protocol_outcome_match
      guardrails: [no_model, no_side_effect]
    budget:
      max_duration_ms: 100
```

### 限制

- Suite 最大 256 KiB、最多 500 cases；ID 使用小写字母开头的 kebab/snake 形式；
- fixture 必须是相对 Suite 目录的普通 JSON 文件，不能使用绝对路径、`..` 或越界符号链接；
- fixture 最大 64 KiB，必须提供 SHA-256，读取后先验 digest 再解析；
- `runner` 本切片只允许 `protocol_hello`；unknown runner 在加载阶段形成 Eval 自身错误；
- `metrics.primary` 固定为 `protocol_outcome_match`，guardrail 固定 allowlist；
- 每 case `max_duration_ms` 为 1..5000；整个 suite 另有 1..60000 ms 总预算；
- extra 字段拒绝，避免把 prompt、secret 或任意命令藏进 Eval 文件。

## 执行语义

1. `HarnessService` 加载 Profile，解析调用者指定的 suite；未指定时运行全部已声明 suite；
2. suite 路径必须与 Profile 声明精确匹配；静态 Eval 不要求 Profile trust，因为不执行命令；
3. loader 有界读取 YAML，严格验证 schema 和 case ID 唯一性；
4. 逐 case 有界读取并校验 fixture digest；
5. `protocol_hello` runner 调用 `normalize_client_record()` 和 `negotiate_hello()`；
6. 实际结果标准化为 accepted/rejected、error code、selected version、capabilities；
7. expected 与 actual 完全比较；达到 case/suite 时间预算立即停止，未运行 case 标记 skipped；
8. 所有结果只在内存中返回，本切片不写 Store 或 artifact。

## 错误分类

- `passed`：生产实现结果与 expected 完全一致；
- `implementation_failure`：fixture 与 suite 有效，但生产协议行为和 expected 不一致；
- `evaluation_error`：suite/fixture/schema/digest/runner/预算自身无效或无法读取；
- `skipped`：suite 总预算耗尽后尚未执行。

结果必须明确区分“产品回归”和“评测资产坏了”，不得用一个 failed 混合。

## 双通道体验

### 用户命令

- `/harness eval`：运行 Profile 声明的全部离线 suite；
- `/harness eval <suite-id|relative-path>`：只运行一个声明 suite；
- 输出 suite、case 总数、passed/implementation failure/evaluation error/skipped、耗时；
- 失败 case 展示 expected/actual 与下一步，成功 case 默认单行，避免刷屏。

### Agent Tool

`harness_eval` 是 read-only、concurrency-safe、无需确认的 Tool；可选 `suite` 字符串参数。
Tool 不接受 live、command、cwd、provider 或任意 fixture path，避免绕过 Profile allowlist。

## 内置真实 fixture

仓库新增 `docs/harness/evals/protocol-hello-core.yaml` 与至少六个 JSON fixture：

1. 现代客户端完全兼容；
2. 能力交集丢弃未知 feature；
3. 旧客户端兼容；
4. 版本区间无交集；
5. 缺 required capability；
6. bool 伪装版本或非法能力格式。

它们直接调用当前 `src/naumi_agent/ui/protocol.py`，因此属于真实 NaumiAgent 小模块而非 mock runner。

## 验收标准

1. schema 正常、重复 ID、extra 字段、unknown runner、危险路径、超限、digest mismatch 均有中文错误；
2. 六个内置 fixture 全部通过，连续运行两次 canonical result（除 duration）一致；
3. 手工篡改 expected 产生 `implementation_failure`，篡改 fixture/digest 产生 `evaluation_error`；
4. `/harness eval`、TUI/New UI 共享 slash router，Agent Tool 调用同一 service；
5. 未配置 suite、未知 suite、Profile invalid 均给出可行动提示；
6. 用真实仓库 `.naumi/harness.yaml` 跑内置 suite，不需要模型/API key，工作树不发生变化；
7. 仅运行定向 Ruff、compileall、Harness Eval 单元/表面/真实工作区测试，不运行全量测试。

