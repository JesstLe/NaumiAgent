# UI-14.2c Task QuickOpen Provider

## 用户结果

New UI 与 Textual TUI 的 `Ctrl+P` QuickOpen 现在支持命令和任务两个 provider。打开后默认仍是
命令列表；按 `Tab` 切换到任务列表，可按任务 ID、标题、Owner、来源、状态和公开 detail 搜索
Todo、子智能体、后台任务与浏览器任务。方向键选择、Enter 填入、Esc 取消，两端键位一致。

选择任务只把 `/tasks detail <id>` 填入 composer，不会自动发送、取消任务或执行其他写操作；
用户仍需检查后按 Enter。取消 QuickOpen 保留原草稿。

## 权威数据与安全边界

- 任务来源是 UI-11.1a 的类型化 `TaskViewItem` / `tasks/snapshot`，不解析任务面板 ANSI 文本，也不
  复制第二套任务事实。
- Python `search_terminal_tasks()` 是 TUI 的有界投影；New UI 消费经过 protocol normalizer 的同一
  公开字段集合。共享 `task-quick-open-golden.json` 锁定两端排序、中文搜索与模板。
- 结果最多 50 项，查询最多 200 grapheme/字符。空查询按运行中、阻塞、等待、失败、取消、完成排序，
  同状态再按来源和稳定 `view_id` 排序。
- task ID 必须符合 ASCII allowlist `A-Z a-z 0-9 . _ : / -`；空白、控制字符、`;`、`&` 等批命令
  分隔符不会进入 QuickOpen，避免用户提交模板时扩展成额外命令。选择模板本身不做 shell 执行。
- 每次重新打开 QuickOpen 后首次切到任务 provider 都读取新的权威快照。仍在途的关联请求不会重复
  发送；响应必须匹配 request ID，无法归属的快照不会污染 provider。

## New UI

- `Tab` 首次切换到任务 provider 时发送有界 `task_panel` 只读请求。
- 与请求 ID 匹配的 `tasks/snapshot` 只进入 QuickOpen 临时缓存，不创建任务面板消息、不抢占任务面板
  焦点、不关闭欢迎页；普通 `/tasks` 请求仍沿用既有任务面板行为。
- 关联错误显示在 QuickOpen 内，保留 composer 与对话；session replacement 会清空任务缓存。
- 状态颜色遵循现有任务语义，同时始终显示“运行中、阻塞、已完成”等文字。

## Textual TUI 与共享命令

- TUI 切换 provider 时直接调用 `build_task_panel_snapshot(limit=50)`，复用四类任务的现有聚合、错误
  降级与工作区边界。
- 读取失败使用固定中文行动提示，不把异常详情写入 UI；部分来源失败时展示 `/doctor` 建议。
- 共享 `/tasks` 后端新增只读 `detail <id>` 参数，CLI/TUI 能执行 QuickOpen 填入的同一模板；无参数
  `/tasks` 的原列表行为保持不变，歧义参数给出精确用法。

## 验收证据

- Python 覆盖四来源排序、中文元数据搜索、非法 ID 排除、limit 边界、安全模板、共享 slash detail
  成功与错误路径，以及真实 Textual app 的 `Ctrl+P → Tab → Enter` 草稿填入链路。
- Node 覆盖关联 snapshot 隔离、错误内联、每次打开刷新、provider 渲染、中文搜索与仅填入合同。
- 真实 Node 终端进程通过 fake Bridge 的严格 request correlation 加载类型化任务，选择后 composer 为
  `/tasks detail sub_1`，协议日志中没有 `submit`。
- 相关 task panel、command index、state 和 component 小模块测试通过；未运行全量测试。

## 当前边界

- UI-14.2 仍为 partial：workspace 文件、会话、Agent 与页面 provider 尚未实现。
- 本切片没有跨启动保存任务结果，也没有把任务 QuickOpen 当成任务面板或全屏详情替代品。
- 历史后台任务默认不加载；用户仍可通过 `/tasks history` 使用既有历史任务面板。
