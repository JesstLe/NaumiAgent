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

- `HarnessReplayService.replay(run_id, *, verify_artifacts=True) -> HarnessReplayResult`
- `HarnessReplayResult.status`: `reproduced|changed|partial|corrupt|not_found`
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
