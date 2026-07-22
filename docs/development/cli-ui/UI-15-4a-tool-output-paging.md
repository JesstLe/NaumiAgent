# UI-15.4a 文本工具输出归档与分页

## 目标

大工具输出不能完整进入 New UI/TUI 状态，也不能因为前端截断而永久丢失。本切片在工具执行完成时将
超过 2,000 字符的文本输出写入会话作用域的不可变归档，事件协议仅携带有界预览、真实长度和不透明
artifact 引用。用户可在 New UI 与 TUI 使用同一命令分页读取。

## 数据流

1. Engine 保留事件中的前 2,000 字符预览。
2. 完整文本按 8,192 个 Unicode 字符切页，写入 runtime data 下的 `tool-outputs` 私有目录。
3. manifest 记录会话、工具调用、字符/字节长度、总摘要和每页摘要。
4. `tool_end` 仅发送 artifact id、页数、页大小、长度和摘要，不发送完整正文或本地路径。
5. 两个 renderer 在工具卡展示 `/tool-output <artifact-id> 1`。
6. 共享 slash command 以当前活动会话为权限边界，按需读取并校验一页。

## 安全与故障语义

- artifact id 是随机不透明标识，命令不接受文件路径。
- 读取必须匹配当前活动会话；跨会话引用拒绝访问。
- 页码、manifest 版本、总摘要格式和每页摘要均失败关闭。
- 归档目录与文件尽力收紧为 `0700/0600`；删除会话时删除其归档，且不跟随符号链接。
- 归档 I/O 失败不会把成功工具改写为失败；UI 仍显示预览和真实长度，但不会虚构可用的分页引用。

## 协议字段

- `content`：至多 2,000 字符的预览。
- `content_length`：完整内容的 Unicode 字符数。
- `content_bytes`：完整内容的 UTF-8 字节数。
- `output_artifact_id`：不透明归档 id。
- `output_page_count` / `output_page_chars`：分页元数据。
- `output_sha256`：完整 UTF-8 内容摘要。

旧 producer 不提供新字段时 adapter 保持兼容；renderer 仅在 artifact id 存在时显示分页入口。

## 验证证据

聚焦测试覆盖 Unicode 多页往返、跨会话拒绝、越界页、篡改摘要、损坏 manifest、安全 Markdown fence、
共享 slash command、New UI/TUI 同一入口、会话精确清理和符号链接边界。

UI-15.6a 的原始 RED fixture 直接把 10MB 正文放入前端状态，`large_output` 冷渲染为
1,048.636ms，RSS 增量为 371,671,040 bytes。生产协议分页后的 v2 fixture 只注入预览和引用，
`paged_output` 冷渲染为 0.916ms，RSS 增量为 49,152 bytes。证据分别保留在：

- `evidence/UI-15-6a-current-renderer-release-darwin-arm64.json`
- `evidence/UI-15-4a-paged-output-release-darwin-arm64.json`

此对比只证明前端不再复制 10MB 正文，不代表后端归档写盘成本或整个进程 RSS。

## 未完成

- 当前分页通过共享命令展示，尚无全屏 artifact viewer、上一页/下一页快捷键和分页复制动作。
- 仅覆盖文本；二进制、图片和结构化 diff 仍需各自的 artifact contract。
- 尚无周期性孤儿清理、损坏归档隔离和容量配额。
- 完整 ToolResult 仍可能留在模型消息历史直到上下文压缩；本切片只解决 UI 传输与状态驻留。

因此 UI-15.4 仍为 partial，UI-15.4a 是可独立验收的文本输出切片。
