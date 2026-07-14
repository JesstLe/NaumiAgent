# Harness H4.4 Explain Design

- 日期：2026-07-15
- 状态：已批准（用户已持续授权 Harness 设计）
- 范围：确定性失败分类、运行解释查询、Agent Tool 与 `/harness explain`

## 1. 目标

让用户和 Agent 能回答“这次 Harness 运行为什么成功、失败、未验证或仍在运行”，
并给出可复核事实、稳定失败分类和下一步行动。解释只读取 `harness.db` 的规范化
记录，不调用模型、不执行工具、不读取或复制原始 stdout、参数、权限原因和 reasoning。

## 2. 方案

新增纯 `HarnessExplainer`，输入一个 `HarnessStoredRun`，输出冻结的结构化
`HarnessRunExplanation`。`HarnessService.explain_run()` 负责当前工作区内按 run id 或
latest 查询；`harness_explain` Tool 和 `/harness explain [run-id|latest]` 只负责参数
校验和渲染，共用 Service 方法。

不把分类结果写回 SQLite。解释完全由持久化事实派生，避免迁移、缓存失效和同一运行
出现两套结论。H4.5 Replay 后续可直接复用纯解释器。

## 3. 输出契约

`HarnessRunExplanation` 包含：

- `run_id`、`status`、`objective`、`started_at`、`completed_at`；
- `failure_classes`：按优先级去重的标准失败分类；
- `findings`：分类、事实来源、中文原因、下一步行动、关联 evidence/check id；
- `check_count`、`evidence_count`；
- `verified`、`running`。

找不到运行时返回结构化 `HarnessExplainLookup`，状态为 `not_found`；没有 Store 时为
`unavailable`。显式 run id 属于其他工作区时必须按不存在处理，不能泄露其 objective、
状态或时间。

## 4. 确定性分类规则

规则按以下顺序运行并保留所有独立问题；同类重复事实合并为一个 finding：

1. Receipt warning 以 `infrastructure_error:` 开头，或 check 为
   `infrastructure_error`/`timed_out` → `environment_error`。
2. check 为 `blocked_by_policy`，或 tool permission 为 denied/rejected/blocked，或
   tool status 为 blocked → `permission_block`。
3. `invalid_tool_call`、Evidence `start_missing` → `tool_contract_error`。
4. check 为 `failed` → `verification_failure`。
5. tool 为 `skipped` → `agent_repetition`；没有明确原因类别的 tool `aborted`/`error`
   → `human_judgment_required`，避免把 Hook 中止或环境问题误报为权限/实现错误。
6. warning 包含缺检查、旧检查、缺证据、验收标准未满足、待对账 Todo、提前完成 →
   `agent_premature_finish`。
7. warning 包含 Profile/契约/完成标准冲突 → `specification_gap`。
8. check 为 `cancelled` → `human_judgment_required`。
9. `completed_unverified` 或 `blocked` 但没有可分类事实 →
   `human_judgment_required`。

`completed_verified` 且没有异常事实时 `failure_classes` 为空，明确显示“验证完成，无已知
失败”。`running` 不是失败，不强行分类；输出“仍在运行或未形成完成回执”。

首版不猜测 `knowledge_gap`、`context_overflow`、`evaluation_error`：当前 Store 没有足够
机械事实，错误归类比不分类更危险。

## 5. 呈现

Markdown 输出固定为：运行摘要、结论、失败分类、为什么、检查与证据摘要、下一步。
每个 finding 必须含中文原因和可执行下一步。Evidence 只显示稳定 id、类型、状态、
digest 前 12 位和 ChatRun URI；不展示 summary 中的参数或结果摘要。

`/harness explain` 默认解释当前工作区最新运行；`latest` 等价于省略 run id。只允许一个
位置参数，多余参数显示中文用法。

## 6. 双通道

- 用户手动触发：`/harness explain [run-id|latest]`。
- Agent 自主调用：只读、并发安全的 `harness_explain` Tool，参数 `run_id` 可选。

两者调用 `HarnessService.explain_run()` 和 `render_harness_explanation()`，不重复分类逻辑。

## 7. 错误处理与安全

- Store 不可用：说明状态库不可用，并提示检查用户状态目录或运行 `/harness doctor`。
- 无运行：说明当前工作区没有记录，并提示先执行一次任务。
- run id 非法：中文参数错误，不触发数据库查询。
- 跨工作区：与不存在同样返回，防止通过已知 id 探测其他仓库运行。
- 数据库损坏：Service 返回安全错误，不暴露 SQLite 路径、SQL 或原始异常。

## 8. 验证

- 纯分类单测覆盖 verified、running、verification failure、permission、repetition、
  tool contract、missing evidence、environment、unclassified blocked。
- Service 测试覆盖 latest、显式 run id、跨工作区隔离、无 Store、损坏 Store。
- Tool 与 slash 表面测试使用真实临时 Git 工作区和 SQLite；从创建 run、记录 Evidence、
  finish 到 `/harness explain` 端到端执行，不 mock 分类器。
- 只运行 Harness Explain、Store、Surfaces、Tools 定向测试，不运行全量测试。

## 9. 非目标

- 不重放工具或模型；
- 不增加 SQLite 表或列；
- 不实现 UI completion receipt card；
- 不做 LLM 补充解释；
- 不实现 Eval 或反馈提升。

## 10. 自审

- 无占位符；输入、输出、分类优先级和边界明确。
- Explain 与 Store、Service、Tool、slash 的职责分离，可被 Replay 复用。
- 不泄露跨工作区记录和原始事件。
- H4.4 单切片可独立测试、提交与回滚。
