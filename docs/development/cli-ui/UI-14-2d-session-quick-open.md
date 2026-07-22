# UI-14.2d Session QuickOpen Provider

## 用户结果

New UI 与 Textual TUI 的 `Ctrl+P` QuickOpen 现按 `命令 → 任务 → 会话` 循环切换。会话 provider
只显示启动工作区内可恢复的会话，支持标题、ID、模型和 Git 分支搜索；当前会话优先，其余按更新时间排列。

选择会话只把 `/load <session_id>` 填入 composer，不自动恢复、不发送消息，也不覆盖打开前草稿。

## 数据与安全边界

- New UI 通过 ARC-03.2b2 的关联 `sessions/list/request → sessions/list` v1 获取快照；响应只进入
  QuickOpen 临时缓存，不创建时间线消息。
- TUI 直接复用同一 `build_session_list_snapshot()` 权威投影，不自行读取 SQLite 或复制过滤规则。
- 消息正文、summary、工作区路径、费用和 Token 不进入 provider；空会话不提供恢复入口。
- session ID 再次通过安全字符合同后才生成 `/load` 模板；选择永不自动执行。
- QuickOpen 每次重开清空会话缓存；它不写入 UI snapshot，也不会在新进程中恢复覆盖层。

## 验收证据

- Node 单元验证关联快照、搜索、当前会话排序、私有字段隔离及只填入行为。
- Node 真实终端进程验证键盘切换、渲染、填入，以及没有 `submit`/`resume` 事件。
- Python 共享搜索/模板测试验证排序和模板。
- Textual 真实 app pilot + SQLite 会话验证两次 Tab、工作区会话显示及只填入。
- 仅运行相关 Ruff、Python 与 Node 小模块测试；未运行全量测试。

## 未完成项

文件、Agent 和页面 provider、跨启动最近历史、可取消后台索引、Vim mode 与键位冲突诊断仍未实现；
UI-14 保持 partial。
