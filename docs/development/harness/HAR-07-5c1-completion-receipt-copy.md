# HAR-07.5c1 完成回执跨平台复制

## 目标

让用户从 New UI、Textual TUI 或 deprecated CLI 使用同一个命令复制一张已经持久化的权威完成回执，
而不是复制整段会话、解析屏幕文本，或依赖某个前端保存的临时卡片状态。

## 用户入口

```text
/copy receipt
/copy receipt latest
/copy receipt <receipt-id>
```

完成卡底部显示精确的 `/copy receipt <receipt-id>`。`latest` 只在当前活动会话内选择按开始时间倒序的
最近一张持久回执，不跨会话猜测。

## 权威与边界

- `ChatRunStore` 是通用 Completion Receipt 的唯一查询 authority；精确查询同时绑定
  `session_id + receipt_id`，另一个会话即使知道 ID 也不能导出。
- 若同 `run_id` 存在 Harness Receipt，只有其 `workspace_root` 与 `session_id` 都和当前 Engine
  完全一致时才合并。Harness Store 暂时故障时仍复制通用回执，并明确标记降级，不泄漏内部异常。
- 导出复用完成卡的共享有界 formatter，只包含卡片已允许展示的摘要、验证计数、Git 概览、风险和下一步；
  不复制 reasoning、原始工具输出、Evidence URI、变更绝对路径、环境变量或凭据。
- Python Bridge 负责查询、渲染、写盘和剪贴板操作；Node New UI 不读取 SQLite，也不持有第二份回执事实。

## 文件与剪贴板语义

- 每次操作都先以 UTF-8 保存到 Engine 权威的 `.naumi/exports`，再尝试系统剪贴板。
- macOS 使用 `pbcopy`，Windows 使用 `clip`，Linux 依次尝试 `wl-copy` 与
  `xclip -selection clipboard`；单个后端最多等待 3 秒，然后继续降级。
- 剪贴板不可用不是数据丢失：命令返回保存路径。文件名前缀经过白名单清洗，使用微秒时间与独占创建，
  并发操作不会覆盖既有导出。

## 跨表面一致性

- `/copy receipt ...` 在共享 `_handle_command()` 内执行，不依赖 `_active_cli` frontend adapter。
  因而 New UI 不再返回“当前界面不支持复制完整记录”，TUI 与 CLI 也走同一实现。
- 命令索引、帮助页和旧 completer 同步公开
  `<all|last|error|receipt [receipt-id|latest]>`；原有 Ctrl+Y 和 transcript scope 不变。
- New UI ANSI 卡与 Textual Rich/Markdown 卡都显示相同的精确复制命令，颜色关闭时命令文字仍完整。

## 验收证据

- 真实 `ChatRunStore` 覆盖 latest、精确 ID、跨会话拒绝、无会话、无安全导出目录和 Harness 降级。
- 共享 Slash Router 在 `frontend=None` 下成功复制；Bridge 路由产生 New UI system notice 并写入
  `.naumi/exports`。
- 文件导出覆盖前缀穿越、20 路并发不覆盖和 Linux 首选剪贴板后端超时后的 fallback。
- Node 完成卡覆盖复制入口、ANSI 关闭和 80/120/200 列宽度边界；Textual formatter 覆盖同一入口。
- 只运行本模块及直接相关测试、Ruff、compile 和 Node syntax/check，不运行全量测试。

## 自我审视与剩余边界

- 本切片完成“跨平台复制回执”，没有把 HAR-07.5 全部标记完成。
- 完成卡目前提供可复制、可编辑的精确命令，不是鼠标点击区域；完成卡直接进入 Harness Detail
  仍属于后续 HAR-07.5c2。
- Windows/Linux 的命令选择已有单元覆盖和失败降级，本轮真实场景运行在 macOS；三平台真实 PTY 与
  系统剪贴板矩阵仍应进入 UI-16 发布验证。
- 本切片不处理客户端 ACK、活动运行恢复或 cursor/revision/gap 自动补发，这些仍属于
  ARC-02.5b / HAR-07.4b。
