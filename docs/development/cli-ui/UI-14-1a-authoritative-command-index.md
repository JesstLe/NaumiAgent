# UI-14.1a Authoritative Terminal Command Index

## 目标

把旧 CLI completer 中已有但未进入产品协议的命令元数据升级为 New UI/TUI 共用的严格索引，为 QuickOpen、命令补全、
权限预览和键位发现提供唯一事实源。执行仍走共享 slash router；本切片不实现 QuickOpen 全屏 provider，也不让前端根据
metadata 绕过运行时权限判断。

## 索引合同

`TerminalCommandIndexEntry` schema v1 对每个命令固定以下字段：

- canonical command、排序去重的 alias、中文说明和稳定 category；
- source：`shared_runtime|new_ui|tui`，区分共享执行命令和真实表面本地命令；
- readonly 与 permission risk：`read_only|session_state|permission_change|workspace_write|tool_execution|destructive`；
- 有界 argument schema：是否接收参数、原始 syntax，以及是否至少要求一个参数。

共享 runtime 条目直接消费 `COMMANDS_META`，不复制命令描述。New UI/TUI 只追加各自真实处理的本地命令；重复 canonical
name、非法 command/alias、空说明、参数声明冲突和“readonly + 非只读 risk”都在构建阶段失败。

argument schema 当前用于展示和风险预览，不替代各命令真实 parser；复杂嵌套子命令仍保留完整 syntax，由 slash router
做最终解析。后续 UI-14.2 如需生成结构化表单，必须逐命令补充 typed field schema，不能从中文提示猜测参数类型。

## 双端消费

- Bridge 的 runtime status 发布完整 New UI index；旧的三字段 registry 仅保留为异常 fallback；
- Node reducer 保留 source/category/risk/arguments，并对 schema v1 独立执行枚举、长度、一致性与 alias 校验；
- New UI 补全框显示参数 syntax、category 和中文风险标签；颜色按只读、会话、写入、执行、破坏性区分，但文字标签始终存在；
- TUI 的 inline suggestion 和 fuzzy candidate 都改为消费 `build_terminal_command_index("tui")`，不再拼接第二份命令名列表。

## 验收证据

- New UI index 完整覆盖全部 `COMMANDS_META` 和 11 个真实本地命令，TUI 只增加 4 个实际处理的本地命令；
- `/help`、`/new` alias，`/write`、`/delete`、`/mode`、`/harness` risk 及必填/可选参数机械断言；
- Bridge status 暴露完整 schema；TUI fuzzy suggestion 同时找到 `/workbench` 和 `/worktree`；
- Node 保留可信 metadata，丢弃伪造 readonly 的 schema v1 条目；100 列无颜色输出仍显示“工作区写入”；
- Python 22 项、Node 44 项定向测试，Ruff、compile 与 diff check 通过；未运行全量测试。

## 当前不足与下一步

UI-14 仍为 partial。下一步 UI-14.2a 应先实现有界 Command QuickOpen provider：按 category/source/risk 查询本索引，选择
只把 command+syntax 写入 composer，写操作不得直接执行。文件、会话、任务、Agent provider、100k 文件后台索引、最近使用
排序和 Vim mode 仍需后续独立切片。
