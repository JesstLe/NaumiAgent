# HAR-05 安全 Replay 详细设计

## 1. 决策摘要

HAR-05 只重放已经持久化的规范化事实，不重放任何工具、模型、检查或会话。系统在
Harness Run 完成时保存一份不可变 Replay 基线；之后由纯回放引擎重建时间线、校验证据
与产物、重新运行版本化分类规则，并比较输入与解释是否发生变化。

本模块不依赖完整 ARC。它只复用已经落地的 Harness Store、Completion Receipt、Evidence
与 `HarnessExplainer`，不会提前实现 ARC-02、ARC-03 或通用生命周期框架。

## 2. 用户闭环

- 用户运行 `/harness replay <run-id|latest>` 查看安全回放回执。
- Agent 通过只读工具 `harness_replay` 调用同一个 `HarnessService.replay_run()`。
- 输出明确区分：完全复现、规则或事实改变、证据不完整、数据损坏、记录不存在。
- 输出只展示规范化元数据、digest、差异和下一步，不展示工具原始输出或损坏内容。

## 3. 安全边界

Replay 引擎没有 `ToolRegistry`、`ModelRouter`、`HarnessCheckRunner` 或 Session 的依赖，因而在
类型与依赖层面无法执行副作用。允许的外部操作只有：

1. 从 Harness SQLite Store 读取 Run/Check/Evidence/Receipt 与 Replay 基线；
2. 读取当前工作区内、基线明确引用的 artifact 文件并计算 SHA-256；
3. 首次处理旧版本 Run 时写入一份不可变基线。

路径解析必须先 canonicalize，再校验位于当前工作区。拒绝 `..` 穿越、绝对路径逃逸和
解析后指向工作区外的符号链接。显式请求其他工作区的 run id 一律按 `not_found` 处理。

## 4. 持久化模型

Harness Store schema 从 v1 升至 v2，仅新增表，不改写现有 Run 数据：

```sql
CREATE TABLE harness_replay_baselines (
    run_id TEXT PRIMARY KEY REFERENCES harness_runs(id) ON DELETE CASCADE,
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    explanation_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

基线字段一经写入不可覆盖。相同内容重复写入幂等返回；不同内容使用相同 run id 时抛出
`HarnessStoreConflictError`。正常的新 Run 在完成后生成基线。历史 v1 Run 第一次 Replay
时补建“旧记录基线”，并在回执中声明基线是本次建立的，因此无法证明基线建立之前的规则
变化；后续回放均可跨进程比较。

## 5. Replay Manifest

Manifest 使用 canonical JSON（UTF-8、key 排序、无空白、禁止 NaN）和 SHA-256。它包含：

- manifest schema version 与 Harness Store schema version；
- run identity、workspace、状态、task kind、时间、tree fingerprint；
- contract、receipt、criteria 的规范化结构；
- 稳定排序的 timeline event 引用；
- check 元数据和可用 artifact 的基线 digest；
- evidence kind、URI、期望 digest、producer、criterion 关联与规范化 summary。

时间线排序键为 `(timestamp, phase_rank, stable_id)`：run start、check、evidence、run finish。
相同时间使用 phase rank 和稳定 id 打破平局。缺少完成事件、运行仍为 running、tool evidence
标记 `start_missing` 等情况写入 anomaly，不伪造事件。

## 6. Artifact 与 Evidence 校验

### `chat-run://`

只接受 URI 中 run id 与 evidence id 同当前记录一致的 `.../tool/<evidence-id>` 形式。对存储
的规范化 summary 使用与 EvidenceCollector 相同的 canonical JSON 算法重算 digest。
URI 不一致或 digest 不匹配均为 `corrupt`。

### `artifact://`

URI path 按当前 workspace 的相对路径解析。文件缺失为 `partial`；存在但 SHA-256 不同为
`corrupt`；一致为 verified。目录、越界路径、不可读文件均不能作为可信证据。

### Check artifact

正常新 Run 的基线保存 artifact 内容 digest。回放时缺失为 `partial`，变化为 `corrupt`。
旧 Run 首次建立基线时如果 artifact 已缺失，只记录 anomaly，不能把缺失状态当作可信基线。

未知 evidence kind 或 URI scheme 视为 `partial`，保留其他可解释事实；格式自相矛盾、digest
非法或数据库 canonical facts 改写则视为 `corrupt`。

## 7. 规则 Replay

`HarnessExplainer` 暴露显式 `HARNESS_EXPLAIN_RULE_VERSION`。基线保存规则版本、规范化解释和
解释 digest。回放用当前规则重新解释同一 Stored Run：

- manifest digest 和 explanation digest 均一致：`reproduced`；
- manifest 一致、规则版本或 explanation 不一致：`changed`；
- manifest 改变或 artifact/evidence digest 损坏：`corrupt`；
- 只有缺失、旧记录首次基线、running、未知可选类型：`partial`。

状态优先级为 `corrupt > changed > partial > reproduced`。`not_found` 与 Store `unavailable`
是查找层状态，不进入可信回放结论。

## 8. Replay Receipt

结构化回执至少包含：run id、最终状态、baseline/current manifest digest、baseline/current
rule version、baseline/current explanation digest、timeline、artifact verification、anomalies、
differences、`legacy_baseline_created`。渲染文案使用中文，并给出与状态匹配的下一步。

回执本身由输入确定，不包含当前墙钟时间，因此同一输入连续 50 次回放结构完全一致。
基线的 `created_at` 只属于持久化审计记录，不参与 Replay Receipt。

## 9. 并发与失败语义

- 基线写入使用 Store 的写锁和 `BEGIN IMMEDIATE`，并发首次回放只能得到一个基线。
- 同一基线的 Replay 是纯读，可并发执行。
- Store 损坏返回 `unavailable`，不得泄露 SQLite 错误、数据库路径或原始记录。
- artifact 在读取过程中变化时，以读取到的字节 digest 为准；不执行重试造成不确定结果。
- Replay 不修复数据、不覆盖基线、不自动重新运行任何检查。

## 10. 验收矩阵

1. 新完成 Run 跨 `HarnessStore` 实例连续回放 50 次，结构化结果完全一致且为 reproduced。
2. 删除 artifact 后为 partial；修改 artifact 后为 corrupt，输出不包含文件内容。
3. 修改纯规则版本/输出的测试替身后为 changed，并同时显示旧/新版本。
4. chat-run evidence URI 或 summary digest 不一致为 corrupt；tool `start_missing` 为 partial。
5. running Run 为 partial；未知 run 与外部 workspace run 为 not_found。
6. slash 与 Agent Tool 渲染同一 `HarnessService` 结果，Tool 元数据为 read-only、
   concurrency-safe。
7. 用真实临时 Git 工作区、SQLite 文件和独立 Python 进程完成跨进程 Replay；过程中工具、
   模型和检查执行计数均为 0。

## 11. 已知限制

- v1 历史 Run 没有完成时基线。第一次 Replay 只能建立诚实的 legacy baseline，不能声称已
  验证此前规则版本。
- `chat-run://` 当前持久化的是规范化事件摘要而非原始 ChatRun payload；Replay 只能验证
  该摘要未被改写，不能恢复原始输出。
- HAR-05 不提供通用 schema migration 框架；这里只实现一个向后兼容、只新增表的局部迁移。
