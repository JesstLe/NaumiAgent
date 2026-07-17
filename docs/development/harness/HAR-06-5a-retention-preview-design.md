# HAR-06.5a Session 保留策略只读预览

## 交付边界

本切片为 HAR-06.5 周期 worker 建立可解释、可验证的安全前置，但不执行自动删除：

- Session 增加独立的 `last_accessed_at` 与 `archived_at`，旧数据库增量迁移并以
  `updated_at` 安全回填；
- 恢复历史会话时原子更新访问时间、恢复为 `active` 并清空归档时间；普通历史预览不更新访问时间；
- SQLite 在单条只读快照查询中只投影候选元数据、全量汇总和载荷字节数，不解析 `messages`
  JSON；单轮最多扫描 10,000 条；
- 纯规划器按过期、会话载荷空间压力或二者共同原因生成最久未访问优先的确定性候选；
- 数量和字节预算都是硬上限，超限候选只延后，不允许单条大记录穿透预算；
- `/history retention-preview`、CLI/New UI、TUI 和
  `session_history(action="retention_preview")` 共用同一结果与中文解释。

本切片不存在自动删除开关。HAR-06.5b 才能增加明确 opt-in 并把本计划交给 retention worker，
而且仍须经过
`archive -> delete` 生命周期决策与既有 Session/Harness/Artifact 协调器。

## 数据语义

| 字段 | Producer | Consumer | 语义 |
| --- | --- | --- | --- |
| `last_accessed_at` | Session Store `save/resume` | candidate scan / planner | 用户最近实际使用或恢复会话的时间 |
| `archived_at` | Session Store `archive` | planner | 最近一次进入归档状态的时间；恢复后清空 |
| `payload_bytes` | SQLite SQL projection | planner / renderer | Session 表该行持久化字段的 UTF-8/SQLite 载荷估计 |

有效最近访问时间为 `max(last_accessed_at, archived_at)`。因此刚归档的旧会话不会立即因旧访问
时间被清理。`payload_bytes` 不包含 Harness 数据库页、SQLite 空闲页、索引和物理 Artifact，界面
必须称为“会话持久化载荷”，不得称为工作区总占用。

## 配置

配置位于 Naumi 的正常配置树 `memory.session_retention`，而不是独立的隐藏状态文件：

```yaml
memory:
  session_retention:
    delete_archived_after_days: 30
    max_archived_session_bytes: 0
    max_sessions_per_pass: 20
    max_bytes_per_pass: 268435456
    scan_limit: 10000
```

`max_archived_session_bytes: 0` 表示不以空间压力选取候选；过期规则仍可预览。6.5a 不接受也不
保存自动执行开关，避免把尚未存在的 worker 配置伪装成已经可用的功能。

## 规划规则

1. 非 `archived`、空 ID、负载荷和当前运行会话失败关闭，不进入候选。
2. 候选按有效最近访问时间升序、Session ID 升序稳定排序。
3. 超过保留期即满足年龄规则；总归档载荷超过非零空间上限时，从最旧记录开始累计到足以释放
   超额载荷。
4. 同时满足两个规则时原因固定为 `age_and_storage`。
5. 在稳定顺序上应用单轮数量与字节硬预算；过大的记录延后并继续检查后续较小记录。
6. 总归档数量超过扫描结果时显示截断警告，不声称已覆盖所有候选。

## 验收证据

- Planner：年龄、刚归档保护、当前会话保护、空间压力、双原因、数量/字节预算、确定性 tie-break
  与 10,000 条边界。
- Store：旧 schema 无损增量迁移、原子 resume、损坏 message JSON 不影响候选扫描、稳定 SQL 排序、
  只读预览不改变持久字段。
- Surface：Engine、CLI/New UI、TUI、Agent Tool 使用同一 renderer；文案明确“不会删除”和统计边界。
- 真实场景：真实 SQLite 创建 active/archived 会话，运行命令预览前后比较数据库内容，候选与原因
  符合策略且无记录被删除。

## HAR-06.5b 后续接口

下一切片在不改变本预览模型的前提下增加：周期调度、可取消批次、时间预算、租约/单实例权威、
每批协调提交、吞吐和延后指标。worker 不得直接执行 SQL delete，必须逐项调用现有协调删除入口；
失败、取消和重启继续使用 tombstone/reconciliation 恢复链路。
