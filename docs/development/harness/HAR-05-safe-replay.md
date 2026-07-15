# HAR-05 安全 Replay 与可重复解释

## 目标

从持久化 Run/Check/Evidence/Receipt 重建规范化时间线并重新运行纯分类器，验证同一输入在
相同规则版本下得到相同解释。Replay 不重放工具副作用，不调用模型。

详细数据模型、安全边界与状态语义见
[HAR-05-safe-replay-design.md](HAR-05-safe-replay-design.md)。

## 子模块

| ID | 子模块 | 产物 |
| --- | --- | --- |
| HAR-05.1 | Replay manifest | run id、schema/rule version、事件引用、digest |
| HAR-05.2 | Timeline assembler | 稳定排序、同时间 tie-break、缺失事件标记 |
| HAR-05.3 | Artifact verifier | URI 存在性、digest 校验、损坏/丢失分类 |
| HAR-05.4 | Rule replay | 调用纯 `HarnessExplainer`，比较旧/新解释 |
| HAR-05.5 | User/Tool surface | `/harness replay <run-id>` 与 `harness_replay` |
| HAR-05.6 | Replay receipt | 输入 digest、规则版本、差异、不可重放项 |

## 建议文件

- `src/naumi_agent/harness/replay.py`
- `src/naumi_agent/harness/replay_models.py`
- `tests/unit/test_harness_replay.py`
- `tests/integration/test_harness_replay_store.py`

## 接口

- `HarnessService.replay_run(run_id) -> HarnessReplayLookup`
- `HarnessReplayResult.status`: `reproduced|changed|partial|corrupt`
- Lookup 层状态：`ok|not_found|unavailable`
- Replay 只读取当前 workspace；显式外部 run id 按 not_found 处理。

## 必测失败路径

- Evidence URI 不存在、digest 不匹配、旧 schema、未知 evidence kind。
- tool_end 缺 start、重复 evidence、运行未 finish、Store 在读取中损坏。
- 分类规则升级导致结果变化；必须显示旧/新规则版本，不能静默覆盖。

## 验收标准

- 相同 manifest 连续重放 50 次，结构化结果完全相同。
- 删除一个 artifact 后得到 `partial`，其他证据仍可解释。
- 修改 artifact 后得到 `corrupt`，不把损坏内容展示为可信证据。
- destructive tool 的 execute 调用计数严格为 0。
- 新 Store 实例、slash 和 Agent Tool 得到相同结果。
- A3 证据：真实临时 Git/SQLite run 完成后，跨进程重放并生成回执。

## 非目标

不重放模型、不重新跑 check、不恢复 Session、不做 Eval 评分。

## 实现状态（2026-07-15）

HAR-05.1-HAR-05.6 已实现：

- Harness Store schema v2 保存不可变 manifest、规则版本和 explanation digest；v1 增量迁移只
  新增 Replay 基线表。
- 新 Run 完成时捕获基线；旧 Run 首次回放明确标记 legacy baseline，避免伪称历史可复现。
- 规范化时间线使用稳定排序；artifact、`artifact://` 与 `chat-run://` evidence 执行路径边界
  和 SHA-256 校验。
- `/harness replay [run-id|latest]` 与只读、可并发的 `harness_replay` Agent Tool 共用
  `HarnessService.replay_run()`。
- Replay 依赖中没有 ToolRegistry、ModelRouter、check 执行或 Session 恢复入口。

### 定向验证

- Ruff：所有 HAR-05 源文件和相关测试通过。
- 单元/相邻模块：Replay、Store、Tool、Surface、Explain、Evidence、Runtime Persistence
  定向测试通过。
- A3：真实临时 Git 工作区和 SQLite 由一个 Python 进程完成 Run，另一个独立进程回放；
  reproduced、artifact 删除 partial、artifact 篡改 corrupt 均通过，且 Harness check 执行
  入口由失败 canary 保护，调用次数为 0。

### 自我审视与限制

- v1 历史记录没有完成时基线，首次回放只能建立 legacy baseline；该限制会在回执中显示。
- `chat-run://` 验证的是已持久化的规范化 summary，不恢复或展示原始工具输出。
- HAR-05 使用局部、只新增表的 v2 迁移；通用状态迁移治理仍属于 ARC-05，未在本模块扩做。
