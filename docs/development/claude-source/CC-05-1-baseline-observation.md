# CC-05.1 Approved Baseline Observation

## 目标与边界

本切片为上游差异监控提供一个确定性、只读的 baseline observation。它把 CC-01.1b 最新已批准
history entry 与当前本地 checkout 审计结果组合为机器可验证 receipt，但不建立第二套基线数据库，
不创建 refresh proposal，也不推进 manifest。

本切片只回答“批准的基线是什么、当前 source 是否已经偏离”。文件级 structural diff、交互行为
回归、影响路由和采纳建议分别留给 CC-05.2-05.5。

## 权威关系

```text
CC-01.1b approved history entry
              │
              ├── manifest / identity digest
              ├── approved Git identity
              ├── license evidence
              └── legacy mapping evidence
                         │
                         ▼
             CC-05.1 read-only observation
                         │
                         └── live SourceAuditResult
```

`SourceRefreshStore.latest()` 会校验完整 history 链，并使用 SQLite `mode=ro` 打开现有数据库。数据库
不存在时返回无基线，不创建目录或空库；只读查询不执行 schema 初始化、迁移、WAL 模式切换或权限
修改。manifest 文件摘要必须与 latest approved entry 完全一致，否则拒绝建立 observation。

## Receipt 契约

`SourceBaselineObservation` schema v1 包含：

- 确定性 `observation_id`；
- approved history entry ID、revision、decision kind、审批者和审批时间；
- source identity schema version、manifest/identity SHA-256；
- 已批准 Git remote/commit/branch/upstream/ahead/behind/dirty identity；
- license path、digest 和已审核声明；
- mapping path、digest 和兼容策略；
- 当前 checkout 的 `SourceAuditResult`；
- `current`、`change_detected` 或 `invalid` 状态。

receipt 不含观察时间；同一批准基线和同一 live audit 事实会生成相同 ID，避免定时监控制造噪声。
source 当前事实变化后 observation ID 会变化，但 approved revision 不会自行前进。

## Mapping Schema 诚实边界

当前 `cc-source-map.json` 没有显式 `schema_version`，因此 observation 固定报告：

- `mapping_format = legacy_unversioned_v1`；
- `mapping_schema_version = null`。

这不是缺省成 v1 的猜测，而是对现有文件事实的明确描述。CC-01.3 在引入逐项 v2 mapping schema
后，必须增加新 contract variant 和兼容测试，不能静默把 legacy map 宣称为 versioned schema。

## 状态语义

- `current`：approved manifest 对真实 source、license 和 mapping 的审计为 `valid`；
- `change_detected`：commit、remote、worktree、license 或 mapping 已变化，审计为 `stale`；
- `invalid`：checkout 不存在、Git 身份不可读，或 evidence 路径越过信任边界。

`change_detected` 和 `invalid` 都是可序列化监控结果，不会覆盖批准基线。命令行在 `current` 时返回
0，在另外两种状态或读取错误时返回 1，便于 CI/人工脚本阻断后续采纳流程。

## 维护者命令

```bash
python3 -m naumi_agent.claude_source.baseline \
  --manifest frontend/terminal-ui/cc-source-map.v2.json \
  --source /Users/lv/Workspace/claude-code \
  --project-root . \
  --store <NAUMI_STATE_HOME>/claude-source.db
```

命令只打印结构化 JSON。正式 state 尚未 bootstrap 时，维护者必须先按 CC-01.1b 导入已审核基线；
CC-05.1 不会把当前 manifest 自动视为已批准。

## 验收证据

- 同一 approved history 与 live checkout 重复观察得到完全相同 receipt；
- baseline 查询前后数据库 mtime 不变，缺失数据库不会被创建；
- 新 commit 使状态变为 `change_detected`，但 history revision 保持不变；
- 缺失 checkout 返回 `invalid`，仍保留批准基线身份；
- manifest 未经审批更新时失败关闭；
- receipt 字段或摘要被修改后严格模型拒绝；
- CLI 对真实临时 Git checkout 输出与 Python read model 相同的 observation ID；
- 当前本地 Claude Code checkout 使用临时批准库得到 `current`，未修改 source、manifest 或正式用户
  state。

## 未完成项

CC-05.2a 已实现结构差异清单并绑定本 receipt 的 `baseline_entry_id` 与 `observation_id`；CC-05.2b-05.6
仍未实现。CC-01.2a 已交付独立 license scope audit，后续 observation 必须用兼容 contract variant
显式引用；CC-01.3 完成后再扩展 versioned mapping 字段，不得破坏 legacy v1 receipt 的严格读取。
