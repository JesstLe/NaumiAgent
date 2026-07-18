# EVO-01.6a Candidate 只读审阅面

## 目标

在不开放实验资格、批准、拒绝或代码修改的前提下，让用户和 Agent 通过同一个服务读取当前
工作区的 Evolution Candidate。该切片把已经落库的证据变成可检查的列表与详情，为后续
Eligibility、Prioritization 和完整审阅页提供稳定的查询边界。

## 范围

- `EvolutionReviewService` 是唯一查询入口，CLI、TUI、新 UI 和 Agent Tool 不各自读取数据库。
- `/evolution list` 支持 query、risk、source 和 limit 过滤。
- `/evolution detail <candidate-id>` 显示候选修订、证据来源、Provider/Model/Platform、机械指标和审计链。
- Agent 可调用只读、并发安全的 `evolution_candidates` Tool，输出与斜杠命令使用同一 renderer。
- Candidate 只允许读取当前工作区；不存在或跨工作区的 ID 不泄露详情。

## 数据与资源边界

- 列表最多从 Store 读取最近 500 个 Candidate，最多展示 100 个；默认展示 50 个。
- 详情最多展示最近 100 个审计事件、200 个 Evidence 引用。
- Provider、Model、Platform 各最多聚合 50 个唯一值。
- 列表不展开 Evidence 引用，避免大候选造成无界分配。
- 输出不复制原始用户对话、secret 或源码；仅展示已经脱敏的 Candidate 字段与内部 Evidence 引用。
- 本切片不写 Store，不产生 audit event，不改变 Candidate revision。

## 交互契约

```text
/evolution
/evolution list --query timeout --risk high --source harness_failure --limit 20
/evolution detail evo_<id>
```

新 UI 与 TUI 通过共享斜杠命令结果展示 Markdown；legacy CLI 保留兼容能力，但不再形成独立实现。
专用全屏候选页、排序解释控件和实时刷新不属于本切片。

## 验收标准

- 空库、过滤命中、过滤为空、详情存在、详情不存在均有稳定中文反馈。
- 斜杠命令和 Agent Tool 使用同一个 `EvolutionReviewService`，且结果一致。
- risk/source/limit/query 的非法输入被拒绝，不暴露底层 SQLite 路径或异常文本。
- 读取前后 Candidate revision、数量和 audit event 数量不变。
- 使用真实 SQLite Store 写入多次同根反馈后，详情显示合并后的 occurrence、revision 和 audit chain。
- 模拟 secret 不出现在列表、详情或错误输出中。
- 仅运行本模块及直接依赖的 focused tests，不以全量测试作为本切片验收前置。

## 后续依赖

- EVO-01.4 Eligibility 必须先提供可审计的确定性资格结果，才能在详情中显示资格原因。
- EVO-01.5 Prioritization 必须提供版本化权重和逐因子解释，才能开放排序。
- EVO-01.6b 才可加入 approve experiment/reject/defer；所有动作必须写入 audit chain 并实现冷却规则。
- UI 专用全屏页应消费同一 Review Service/typed payload，不得直接访问 SQLite。
