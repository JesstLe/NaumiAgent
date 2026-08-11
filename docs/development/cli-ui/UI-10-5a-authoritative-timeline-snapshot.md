# UI-10.5a Workbench 权威 Timeline 快照

## 目标

把 `WorkbenchStore.workbench_audit_events` 中已经持久化、已经脱敏的会话事实投影到 New UI 与
Textual TUI。Timeline 不解析聊天文案，不监听终端 stdout 猜测事件，也不建立第二套日志 Store。

本切片只交付只读快照消费。revisioned 增量事件 producer、断线 cursor 和 push stream 属于
UI-10.5b/HAR-10 后续切片，不在此处伪装完成。

## 权威链路

```text
WorkbenchStore.append_event()
  -> WorkbenchService.dashboard_snapshot(session_id)
  -> events（timestamp DESC，最多 50 条）
  -> JSONL Bridge workbench/snapshot
  -> New UI 严格 normalizer / Textual 严格 validator
  -> Timeline 列表与选中详情
```

- Store 在写入前调用 `redact_event_payload()`；前端仍不信任输入并再次限制字段。
- Service 是唯一快照 producer，事件与任务、worktree、review 共用同一 `stream_id/revision`。
- New UI 和 TUI 只做分类、显示与本地选择，不修改事件、不授予执行权。

## 数据契约

单条事件必须包含：

- `id`：1..128 字符；
- `session_id`：1..500 字符，必须属于当前快照会话；
- `type`：1..160 字符；
- `actor`：1..500 字符；
- `subject_id`：1..500 字符；
- `timestamp`：1..100 字符；
- `severity`：`info|warning|error|critical`；
- `correlation_id` / `parent_event_id`：可空，非空时最多 128 字符；
- `payload`：最多 20 个顶层字段，key 最多 80 字符。

事件身份文本拒绝首尾空白与控制字符；payload 值在显示边界清除控制字符、折叠空白并限制为 500 字符。
payload 只把 string/number/bool/null 投影为公开值；
数组显示项目数，嵌套对象显示“结构化对象”，不把任意深层内容带进终端。整个 Timeline 最多接收
100 条；当前后端快照上限为 50 条。

## 交互与视觉

- 为保持既有 `4 Release` 快捷键兼容，Timeline 追加为 `5 Timeline`，也支持 `l`。
- `Tab/Shift+Tab` 在五个 Workbench 页签间循环。
- `↑/↓`、`Home/End`、`PgUp/PgDn` 导航；刷新后优先按稳定 event id 保留选择。
- New UI 宽屏使用列表/详情双栏，窄屏纵向降级，80/120/200 列不溢出。
- 颜色/符号语义：权限黄、Git 绿、Harness 紫、Agent 青、工具蓝、工作台白；warning 黄，
  error/critical 红，并覆盖普通类别色。TUI fallback 同步使用彩色 emoji 标识，关闭颜色后仍保留完整文本。
- 空态明确说明 Timeline 只显示持久事实，不从聊天文本推断。

## 验收标准

- [x] 真实 Store 事件通过 Service 快照进入 Timeline，顺序保持后端 `timestamp DESC, rowid DESC`。
- [x] New UI 与 TUI 使用同一 `events` 快照，不直接查询 SQLite。
- [x] 0/1/100 条事件均有界；选择在同 stream 新 revision 中按 event id 保留。
- [x] 身份字段控制字符、非法 severity、超过 100 条事件或超过 20 个 payload 字段失败关闭；payload
  文本控制字符在显示边界安全归一化。
- [x] 嵌套 payload 不展开，后端私有字段不会由 Timeline 递归渲染。
- [x] New UI 在 80/120/200 列完成语义色与宽度验证。
- [x] Textual TUI 完成页签、上下导航、空态和错误路径验证。
- [x] `/workbench` 首帧仍为只读，不创建任务、Agent 或 worktree。

## 未完成边界

- UI-10.5b：Bridge revisioned domain event producer、连续 patch 应用、gap full-snapshot 恢复和断线 cursor。
- HAR-10：多实例 push notification、持久 cursor 与跨 Store 原子 terminal commit。
- 事件 payload 的领域专用详情卡仍需按事件类型逐项建立 typed projection；本切片不把通用 payload
  摘要冒充领域完整证据。
- Timeline 当前是最近 50 条快照窗口，不提供历史 cursor 翻页或外部审计 archive。
