# UI-14.2f Agent QuickOpen Provider

## 用户结果

New UI 与 Textual TUI 的 `Ctrl+P` QuickOpen 现按
`命令 → 任务 → 会话 → 文件 → Agent` 循环切换。Agent provider 支持按名称、说明、运行状态、类型、
模型 tier、权限级别、能力与工具搜索当前会话的 Agent。

选择结果只把 `/agents agent <quoted-name>` 填入 composer。用户显式提交后，Agent Control Center
打开 Agent 标签并定位该 Agent；QuickOpen 本身不启动 Agent、不停止执行、不发送模型消息。

## 权威状态与协议

- Python 与 New UI 都只消费既有 `AgentControlService` 的 schema v2 `AgentDescriptor`，不读取
  `SubAgentManager` 私有字段，也不建立第二套 Agent 状态；
- `agents/request` 新增 `subscribe` 布尔合同。正常 Agent Control 页面保持 `subscribe=true` 的实时更新；
  QuickOpen 使用 `open=true, subscribe=false` 获取 request-id 关联的 one-shot snapshot；
- one-shot 请求不会修改 Bridge 的 `_agents_subscribed` 或 `_agents_snapshot`，因此关闭 QuickOpen 后不会继续产生
  `agents/update`；
- New UI 将关联快照放入 QuickOpen 临时缓存，不打开 Agent 页面、不覆盖全局 Agent snapshot；
- 用户提前关闭 QuickOpen 时，请求 ID 进入最多 20 项的丢弃表；迟到 snapshot/error 被关联消费但不落入全局页面；
- TUI 直接调用同一个 `engine.agent_control.snapshot()`，不经过本地复制的执行/消息/黑板扫描。

## 搜索与深链安全

- provider 最多读取 100 个 Agent、返回 50 个结果、查询最多 200 字符；
- 排序依次考虑 exact name、name prefix、name substring、结构化 metadata 与 fuzzy subsequence；
- 空查询按 `running → ready → spawned → idle → uninitialized → destroyed` 排列，同状态下动态 Agent 优先；
- Agent 名称最多 200 字符且不得含控制字符；包含空格、Unicode 或引号时由 POSIX quoting 安全编码；
- New UI 与 TUI 都只接受 `/agents agent <name>` 三段深链。歧义参数不会转交模型，而是显示精确用法；
- 深链仍是只读导航模板。停止 Agent 继续要求进入控制中心、切换执行标签并走原有确认/权限路径。

## 双端体验

### New UI

- 加载、错误、部分数据 warning、revision 和 one-shot 状态均在 overlay 内显示；
- Agent 行按状态使用不同颜色，展示动态/预置、任务数与模型 tier；
- 关联 snapshot 先由严格 schema v2 normalizer 校验，再进入搜索；
- 提交深链后设置 `selectedTab=agents`、目标 selection 与 detail，并正常开启 Agent 页面订阅。

### Textual TUI

- `CommandQuickOpenScreen` 复用 Python 搜索与模板 helper；
- `AgentControlScreen` 支持 `initial_tab` 与 `initial_id`，首次 TabActivated 不再误清除深链 selection；
- 真实 modal callback 仍只填 composer；第二次显式 Enter 才打开 Agent Control Center。

## 验收证据

- Python 验证状态排序、中文 metadata、动态 Agent、空格名称、quote round-trip、非法控制字符和 limit；
- Bridge 真实 Engine 验证 one-shot snapshot 返回后无 subscription、无后续 `agents/update`；
- New UI 验证关联隔离、全局 snapshot 不污染、渲染、只填入、显式深链、迟到响应丢弃与歧义用法；
- Textual Pilot 真实执行四次 Tab、选择动态 Agent、只填入深链，再显式提交并定位详情；
- Command index 同步公开 `/agents [agent <name>]` 可选参数；
- 仅运行相关 Ruff、compile、Python/Textual 与 Node 小模块测试，不运行全量测试。

## 未完成边界

- Agent QuickOpen 当前只索引 Agent descriptor；执行、团队消息和黑板仍由 Agent Control Center 展示，
  不在本 provider 中混合不同 ID namespace。
- overlay 是一次性快照，不实时刷新；需要实时状态时应提交深链进入 Agent Control Center。
- 页面 provider、跨启动最近历史、Vim/input mode、typed argument form 与键位冲突诊断仍未实现，
  UI-14 保持 partial。
