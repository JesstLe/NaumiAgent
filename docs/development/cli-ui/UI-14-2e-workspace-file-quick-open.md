# UI-14.2e Workspace File QuickOpen Provider

## 用户结果

New UI 与 Textual TUI 的 `Ctrl+P` QuickOpen 现按 `命令 → 任务 → 会话 → 文件` 循环切换。
文件 provider 在后台建立当前启动工作区的索引，支持相对路径、文件名、目录与扩展名搜索。选择结果只把
`/read <quoted-relative-path>` 填入 composer，不读取、不发送，也不绕过权限检查。

构建期间输入与动画事件循环保持可响应；按 Esc 会取消当前查询与仍在进行的索引构建。新查询会取消旧查询 waiter，
但复用同一个不可变构建任务，避免每个按键重复扫描工作区。

## 权威索引与边界

- Engine 持有唯一 `WorkspaceFileIndex`，New UI Bridge 与 TUI 共用同一工作区、同一 revision；
- Git 工作区通过 `git ls-files --cached --others --exclude-standard -z` 获取 tracked/untracked 且
  ignore-aware 的真实文件集合；非 Git 目录使用不跟随 symlink 的文件系统 fallback，并排除 `.git`、`.naumi`、
  虚拟环境、依赖与常见构建缓存目录；
- 最多索引 100,000 个文件、16 MiB UTF-8 路径数据；单路径最多 4,096 字符、查询最多 200 字符、
  单次最多返回 200 项；
- 排序在线程池执行，100k 结果打分不占用 asyncio/Textual 输入事件循环；
- 协议只传相对 POSIX 路径，不传 workspace root 或绝对路径。New UI 严格拒绝绝对路径、`..`、控制字符、
  Windows drive path、超界集合及与路径不一致的模板；
- 模板由 Python `shlex.quote()` 生成。QuickOpen 只采用协议校验后的模板，用户仍须显式 Enter 提交 `/read`。

## 双端行为

### New UI

- `workspace/files/request` 是 request-id 关联的后台查询；连续输入只接受最新 response；
- `workspace/files/cancel` 显式终止后台构建，关闭 overlay 同时清除本地 pending 状态；
- 页面显示加载、错误、索引来源、总文件数、revision 与截断状态；
- 文件路径属于敏感字段，协议 registry 要求日志链路做 redaction，结果不进入 timeline 或 UI snapshot。

### Textual TUI

- `CommandQuickOpenScreen` 直接调用 Engine-owned index，不复制扫描或排序规则；
- 查询 task 可取消，过期结果必须同时匹配当前 provider 与当前 query 才能渲染；
- Modal 卸载时清理 waiter；若构建仍在进行则请求索引取消；
- 选择 callback 仍然只更新 `#msg-input`，不会启动 Agent。

## 验收证据

- 真实临时 Git 仓库验证 `.gitignore`、tracked/untracked、Unicode/空格路径与无绝对路径泄漏；
- 非 Git fallback 验证 100k 边界机制、依赖目录排除、symlink 不跟随；
- 慢构建验证取消、`CancelledError` 传播及随后可重新构建；
- 两个并发工作区验证索引结果不串线；
- Bridge 验证 request-id 关联、相对路径和 `/read` 模板；
- New UI 验证严格协议、过期响应拒绝、文件渲染与只填入行为；
- Textual Pilot 真实执行三次 Tab、等待后台索引、选择 Unicode/空格文件并确认 Agent 仍空闲；
- 仅运行上述 Ruff、Python、Textual 与 Node 小模块测试，没有运行全量测试。

## 已知限制与后续

- 非 Git fallback 只应用固定重目录排除，不解释任意 `.gitignore`；需要完整 ignore 语义的目录应初始化 Git，
  或后续引入跨平台 ignore matcher。
- 当前索引按打开 QuickOpen 时显式 refresh；尚未接入文件系统 watcher，因此打开期间发生的文件变化不会实时推送。
- New UI 真实子进程测试在本地被既有欢迎页前置断言阻塞：进程直接显示主界面而测试仍等待欢迎页。
  单元级真实 reducer/render/protocol 与 TUI Pilot 已通过，但该既有测试问题不作为 UI-14.2e 的成功证据。
- Agent、页面 provider、Vim mode、typed argument form 与键位冲突诊断仍未实现，UI-14 保持 partial。
