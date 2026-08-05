# CC-03.2a Snapshot 字段语义映射

## 目标

把 CC-03.1b 的 24 个行为单元格先按权威边界分为 Bridge snapshot、UI-local 和 N/A，再为 Task、
Permission、Doctor 三个 Python→Bridge snapshot 建立机器可校验的字段和值域映射。

本切片不把 loading、focus、keyboard、cancel 等瞬态状态塞入服务端 payload。它们属于 CC-03.2b 的
前端状态映射；CC-03.2a 只回答：服务端已经产出的 typed snapshot 中，哪些字段承载 source-like
语义，真实 serializer、协议事件和目标测试是否仍与清单一致。

## 交付物

- `frontend/terminal-ui/cc-semantic-mapping.v1.json`
  - 绑定 CC-03.1b behavior matrix 与当前 protocol contract 摘要；
  - 为 24 格逐一指定 `bridge_snapshot`、`ui_local` 或 `not_applicable`；
  - 为三个 snapshot 声明 event、producer、字段类型、枚举值域和 source→target 值映射；
  - 不记录 Claude Code 源码正文。
- `src/naumi_agent/claude_source/semantic_mapping.py`
  - 运行真实 `TaskPanelSnapshot.to_protocol_dict()`、`permission_panel_payload()` 和
    `doctor_health_payload()` probe；
  - 校验字段存在、实际类型、枚举完整值域、server event 注册与目标测试；
  - 执行 `_canonical_status()`、`_severity()` 和权限风险枚举映射，拒绝值转换漂移；
  - 输出确定性 digest 和稳定 finding code；
  - 全程只读，不启动 Bridge、模型、浏览器或 daemon worker。
- `tests/unit/test_claude_source_semantic_mapping.py`
  - 正常、binding stale、字段缺失、值域漂移、event 缺失、测试缺失与 CLI 只读路径。

## 24 格权威分责

| Area | Bridge snapshot | UI-local | N/A |
| --- | --- | --- | --- |
| Doctor | detail, error, presentation | cancel, empty, focus, keyboard, loading | - |
| Permission | detail, empty, error, presentation | focus, keyboard, loading | cancel |
| Task | empty, presentation | cancel, detail, error, focus, keyboard, loading | - |

关键边界：

- `loading` 表示请求发出后、对应 snapshot 到达前的前端瞬态，不能写进权威 snapshot；
- Task detail 的选择和请求关联是 UI-local，但返回的详情内容继续复用 `payload.items`；
- Task cancel 是独立 `task_cancel` 控制请求，不是只读 `tasks/snapshot` 字段；
- Permission cancel 对只读策略中心不适用；待决权限拒绝属于独立 permission interaction；
- Doctor probe cancel、export、trace 有自己的事件与终态回执，不混入 `doctor/health`。

## Snapshot 字段合同

### `tasks/snapshot`

权威 producer：`TaskPanelSnapshot.to_protocol_dict()`。

| 字段 | 类型/值域 | 语义 |
| --- | --- | --- |
| `payload.schema_version` | literal `1` | Task snapshot schema |
| `payload.items` | array | 四类任务来源的统一列表；空数组是权威空态 |
| `payload.items[].status` | blocked/cancelled/completed/failed/pending/running | 跨来源统一状态 |
| `payload.items[].raw_status` | string | 来源原始状态，仅用于详情和诊断 |
| `payload.items[].title` | string | 有界敏感标题 |
| `payload.items[].owner` | string | 负责人或子智能体名称 |
| `payload.items[].dependency_ids` | array | 稳定依赖身份 |
| `payload.filters.detail_id` | string | 详情请求身份，不解析展示文本 |
| `payload.warnings` | array | 部分来源失败的有界告警 |

Source Task 状态映射：`pending→pending`、`in_progress→running`、`completed→completed`。blocked、failed、
cancelled 是 Naumi 多来源统一状态的扩展，不能从 Claude TaskList 的三态倒推。

### `permissions/snapshot`

权威 producer：`permission_panel_payload()`。

| 字段 | 类型/值域 | 语义 |
| --- | --- | --- |
| `payload.schema_version` | literal `1` | Permission snapshot schema |
| `payload.pending` | array | 待决请求；空数组是明确空态 |
| `payload.grants` | array | 当前会话有效授权 |
| `payload.history` | array | 最近终态决定回执 |
| `payload.pending[].status` | string | 请求当前状态 |
| `payload.pending[].reason` | string | 有界用户解释，不作为策略分支条件 |
| `payload.pending[].policy.source` | string | Python 权限规则来源 |
| `payload.pending[].policy.risk` | low/medium/high | `PermissionRiskLevel` 值域 |
| `payload.warnings` | array | 部分权威来源读取失败告警 |

Claude 的 rule/hook/classifier 表达与 Naumi 的 `TOOL_PERMISSIONS`、`PREFIX_PERMISSIONS`、动态 MCP
策略并非同一个后端模型。本切片只对齐“解释必须结构化呈现”的语义，不声称 hook/classifier 字段等价；
这项差异必须进入 CC-03.4 divergence log。

### `doctor/health`

权威 producer：`doctor_health_payload()` / `DoctorHealthSnapshot`。

| 字段 | 类型/值域 | 语义 |
| --- | --- | --- |
| `payload.schema_version` | literal `1` | Doctor Health schema |
| `payload.status` | ok/degraded/error/unknown | 全局最严重状态 |
| `payload.items` | array | 有界诊断项目 |
| `payload.items[].severity` | ok/degraded/error/unknown | 单项严重度 |
| `payload.items[].domain` | 9 个稳定领域 | 展示分组与归因 |
| `payload.items[].responsibility` | 5 个稳定责任方 | 用户下一步归责 |
| `payload.items[].detail` | string | 脱敏、有界详情 |
| `payload.items[].diagnostic_code` | string | 稳定机器诊断码 |
| `payload.snapshot_sha256` | string | export preview/write 关联摘要 |

Source Doctor 状态映射：`pass→ok`、`warn→degraded`、`error→error`；`unknown` 是 Naumi 对缺失或无效
证据的失败关闭状态。

## 校验命令

必须使用项目锁定环境，避免系统 Python 缺失运行时依赖：

```bash
uv run python -m naumi_agent.claude_source.semantic_mapping \
  --mapping frontend/terminal-ui/cc-semantic-mapping.v1.json \
  --matrix frontend/terminal-ui/cc-behavior-matrix.v1.json \
  --protocol-contract frontend/terminal-ui/protocol-contract.json \
  --project-root .
```

成功报告必须满足：

- `status=valid`；
- `responsibility_count=24`、`projection_count=3`；
- `field_count=verified_field_count=27`；
- `verified_target_test_count=3`；
- 运行前后 mapping、matrix、protocol contract 与目标源码保持不变。

## 验收标准

- 24 格任一缺失、重复或 projection 重复认领时 schema 拒绝；
- matrix N/A 与 mapping `not_applicable` 不一致时报告 invalid；
- matrix 或 protocol contract 摘要变化时报告 stale；
- server event 未注册或 registry metadata 缺失时报告 invalid；
- 真实 serializer 缺少声明字段、类型变化或枚举值域漂移时报告 invalid；
- source→target 值转换与清单不一致时报告 invalid；
- 目标测试被重命名时报告 invalid；
- 同一输入重复运行产生同一 audit；
- verifier 不连接网络、不执行模型请求、不写入项目或用户状态。

## 自我审视与未完成项

- 2a 已验证真实 producer 输出，但只使用一个确定性非空 probe；极端 payload 组合仍由各 producer 的模块测试负责。
- Permission status 当前是开放 string，而不是严格 enum；在后续版本收紧前不能宣称完整值域稳定。
- Permission hook/classifier 的结构化来源在 Naumi snapshot 中尚无等价字段，必须在 CC-03.4 明示语义损失。
- loading/error transport、focus、keyboard、detail selection、cancel correlation 已由后续 CC-03.2b 建立
  独立 UI-local manifest 与真实 JavaScript transition probe；不回填进本切片的服务端 snapshot。
- 组件是否只消费这些字段属于 CC-03.3；本切片不以字段存在替代消费边界证明。

后续切片：`CC-03-2b-ui-local-state-mapping.md` 已完成 14 格 UI-local 映射并保持 Permission cancel 的
N/A 边界；下一步进入 CC-03.3 组件消费边界适配。
