# UI-14.2b 最近命令排序

## 1. 用户结果

New UI 与 Textual TUI 在本次启动期间会把用户真正提交过的已知斜杠命令排到 Command QuickOpen 前部，并显示“最近”标签。
重复使用会提升到首位，别名统一归一化为权威命令名；输入搜索词后，匹配相关性仍优先，历史只用于同分排序。

## 2. 数据与隐私合同

- 仅记录最多 20 个规范命令名，例如 `/write`；参数、文件路径、问题、token 和其他用户文本一律不保存；
- `/h` 等别名在写入前转成 `/help`，未知命令和普通文字不进入记录；
- 相同命令只保留一项，最近一次使用移动到首位；
- 记录仅存在当前前端进程内。新启动回到空历史，符合“未显式 resume 时使用全新瞬态 UI 状态”的产品原则；
- QuickOpen 选择本身不算“使用”，只有用户随后提交该命令才更新排序，取消选择不产生历史。

## 3. 双端实现

Python `record_recent_terminal_command()` 与 Node `recordRecentCommand()` 使用同一 fixture 验证规范化、去重、上限和隐私。
Python `search_terminal_commands()` 与 Node `searchCommandEntries()` 都按 `搜索分数 → 最近序号 → 命令名` 排序；空查询时所有
搜索分数相同，因此最近命令自然置顶。TUI 在命令通过 busy/queue 入口并被实际接受时记录，New UI 在统一
`handleSubmitText()` 入口记录。

## 4. 验收证据

- 共享 `tests/fixtures/ui14/command-recency-golden.json` 包含别名、重复命令、未知命令和带敏感参数的提交；
- 双端只产生 `[/write, /help]`，序列化结果不包含 fixture 中的 private 参数；
- 空查询最近项置顶，`h` 查询仍由 `/help` 相关性胜出；
- Python 定向测试 10 项、Node QuickOpen 测试 7 项通过；
- Node 真实进程的 Ctrl+P、取消、填入不执行测试通过；Ruff、compile 与 diff check 通过；未运行全量测试。

## 5. 当前边界

UI-14.2 仍为 partial。UI-14.2c-14.2f 已分别补齐任务、会话、文件和 Agent provider；本次启动最近命令
仍不跨启动持久化，页面 provider 与跨 provider 合并也未实现。后续能力仍必须使用各自的有界索引、取消和隔离合同，
不能把命令参数或完整输入历史误当成 QuickOpen 数据源。
