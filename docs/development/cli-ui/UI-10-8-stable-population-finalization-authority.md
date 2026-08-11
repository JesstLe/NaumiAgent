# UI-10.8 Stable Population Finalization Authority

## 1. 目标与依赖决策

本切片把 `EVO-05.5f5x3j` 已形成的 Stable Population Finalization Receipt 作为只读权威状态投影到
Workbench，并由 New UI 与 Textual TUI 同源展示。它是跨文档选择的最小用户可见闭环：不继续扩写 x3
transport，也不新增执行、配置/数据 finalization 或 promotion 权限。

依赖：

- `UI-10.1`：Workbench revisioned snapshot 与 Bridge 请求链路；
- `UI-10.2`：New UI 全屏 Workbench；
- `UI-10.7`：Textual TUI fallback；
- `EVO-05.5f5x3j`：Population Receipt、动态 View 与撤权原因。

## 2. 权威边界

唯一后端事实来源是 `EvolutionStableRemotePopulationFinalizationService.latest_view()`。该只读入口只读取
current stable Population Snapshot 的最新 Receipt，不创建 Receipt、不更新 Store、不启动远端任务。

`StablePopulationFinalizationWorkbenchReader` 在 Workbench 边界重新 strict round-trip x3j View，并输出
`WorkbenchStablePopulationFinalizationProjection`。前端不得读取 Evolution SQLite、不得自行比较 Snapshot、
member Receipt 或时间戳，也不得从工具输出文本推断 authority。

## 3. Typed projection

投影只有三个状态：

| 状态 | 历史事实 | 当前权威 | 标识与成员计数 | 撤权原因 |
|---|---:|---:|---|---|
| `pending` | false | false | 必须为空且为 0/0 | 必须为空 |
| `completed` | true | true | Receipt/Snapshot/digest 完整，成员数等于 denominator | 必须为空 |
| `revoked` | true | false | 同 completed | 必须至少一项 |

严格约束：

- schema 固定为 1，未知字段拒绝；
- Receipt/Snapshot ID 与 SHA-256 必须匹配固定格式；
- member 数量范围 0..10000，terminal 状态必须全部完成；
- 撤权原因只允许 x3j 的五个稳定枚举、去重且排序，最多五项；
- `config_data_finalization_authority=false` 与 `promotion_authority=false` 是不可提升的 literal；
- 输入值不会进入验证错误正文，避免密钥、路径或原始 Store 内容泄露。

## 4. Workbench snapshot 与失败语义

`AgentEngine` 将 reader 注入现有 `WorkbenchService`。每次 `dashboard_snapshot()` 在同一个只读请求中附加：

- `stable_population_finalization`：有效 typed projection 或 null；
- `stable_population_finalization_error`：空字符串或固定
  `stable_population_finalization_unavailable`。

source 缺失、Snapshot 不可用、Store 错误或 projection 篡改均降级为固定错误码。Workbench 其余任务、
worktree、review 数据继续可用；日志只记录异常类型，不把异常文本发往 UI。

现有 `workbench/request` 与 `workbench/snapshot` capability 足以承载该 additive 字段，因此本切片不新增
Bridge 事件或协商权限。

## 5. New UI

Node protocol 对 projection 再做 exact-key、类型、边界、identity、状态组合和 authority literal 校验。
非法 projection 会拒绝整条 snapshot，不以宽松默认值制造“已完成”外观。

Workbench 增加 `4 Release` 页签：

- `4` 直达；Tab/Shift+Tab 纳入四页循环；
- completed 使用绿色，revoked 使用红色，pending/unavailable 使用黄色；
- 显示 Candidate、member 完成数、Population Snapshot、Receipt、短 digest、完成时间及中文撤权原因；
- 始终显示“配置/数据 finalization：否 · Promotion：否”；
- 80/120/200 列均有界渲染，不展开 member 清单或原始证据。

## 6. Textual TUI parity

Textual TUI 使用同一 Workbench snapshot，增加相同的 `4 Release` 页签和四页键盘循环。Overview 同时提供
紧凑的 release 状态，用户无需猜测是否已经形成 Population authority。TUI 边界使用同一个 Pydantic
projection 重验，projection 与 error 同时存在时失败关闭。

页签、选择和滚动仍是本次启动内状态；新启动或切换 Session 不恢复 Release 页签。

## 7. 验收标准与证据

- [x] 真实 x3j 两成员 Receipt 投影为 completed；无 Receipt 投影为 pending；Snapshot 变化投影为 revoked；
- [x] Workbench Service 接受合法 typed projection，拒绝伪造 promotion authority；
- [x] source 异常只返回固定 unavailable，不泄露异常中的路径或 token；
- [x] Engine 默认组合完成 reader 注入；
- [x] Bridge → Node normalizer/reducer → Release renderer 的真实 SQLite 链路通过；
- [x] New UI completed/revoked/unavailable 在 80/120/200 列通过；
- [x] Textual `4` 键进入同源 Release 页，completed/revoked/伪造权限路径通过；
- [x] 只运行相关小模块测试、Ruff、语法/编译与文档治理，不运行全量测试。

## 8. 明确未完成

- 没有公开透明日志、exporter 或跨控制面签名 envelope；
- 没有远端 active pointer 的 fresh probe，继续保留 x3j 的 unverified 边界；
- 没有历史 Receipt 列表、成员级 drill-down 或 Timeline 事件；
- 没有 config/data finalization 与 promotion 写动作；
- UI-10.5 Timeline 和 UI-10.6 waiting Approval 动作仍是独立模块。

下一步应重新检查 UI/Harness/ARC/Evolution 文档依赖，再选择新的最小前置；不得借 UI-10.8 继续扩张
x3 transport 或把只读 authority 伪装成发布控制面。
