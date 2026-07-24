# UI-14.2a Command QuickOpen Provider

## 用户能力

New UI 与 Textual TUI 均可通过 `Ctrl+P` 打开命令 QuickOpen。用户可以按 canonical command、alias、中文说明、
category、权限风险或 fuzzy 子序列搜索，使用 `↑/↓/Tab` 导航、`Esc` 取消、`Enter` 选择。

选择只把 `command + argument syntax` 作为可编辑模板写入 composer，不发送、不调用 slash router，也不绕过权限层。
取消不会改变原草稿和光标；运行期间出现权限请求或模型交互时，New UI 主动关闭覆盖层，让阻塞请求取得焦点。

## 权威边界

- Python `search_terminal_commands()` 消费 UI-14.1a 的 `TerminalCommandIndexEntry`，为 TUI 提供有界 1..200 结果；
- New UI 在本地对 Bridge 已校验的同一 metadata 做等价即时排序，避免每次按键产生协议往返；
- 搜索只读取静态、有界、无 secret 的命令 metadata，不索引历史输入、工具输出或 workspace 内容；
- 参数 syntax 是编辑模板，不是授权。命令真正提交后仍由共享 slash router、parser 和 PermissionChecker 裁决；
- QuickOpen 是临时 UI 状态，不进入 session snapshot，不会在重新启动或 resume 时自动恢复。

排序按 exact canonical、exact alias、prefix、substring、中文/英文 metadata、command fuzzy、alias fuzzy、metadata fuzzy
逐级降权，并以 canonical command 稳定打破同分。查询最多 200 grapheme，结果最多 200；TUI 初始显示最多 50 条，
避免交互线程被无界 metadata 拖慢。

## 双端实现

### New UI

- `command-quick-open.js` 管理 open/query/selection/accept/cancel 状态，组合字符按 grapheme 删除；
- `command-quick-open-page.js` 提供全屏覆盖层、可视窗口、中文 category/risk 和“不会自动执行”文字提示；
- 页面始终补齐终端 body 高度，满足增量绘制器的固定行数不变量；
- `Ctrl+P` 可从现有页面打开，传统控制字节与 Kitty keyboard CSI-u 编码均可识别，Esc/Ctrl+P 返回原页面；
  权限/交互请求优先关闭覆盖层；
- 页脚公开快捷键，overlay 打开时隐藏 composer，避免用户误以为 Enter 会发送。

### Textual TUI

- 共享 keybinding registry 新增可覆盖的 `open_command_quick_open`，默认 `Ctrl+P`；
- `CommandQuickOpenScreen` 使用 Input + ListView，键盘与点击均返回同一 editable template；
- Modal callback 只设置 `#msg-input.value` 并聚焦，不发布 `UserInputMessage`，因此不会启动 Agent。

## 验收证据

- exact alias `/h`、fuzzy `wr`、中文风险“工作区写入”、参数模板和 limit 边界均有机械断言；
- Textual Pilot 真实执行 `Ctrl+P → wr → Enter`，验证 `/write <path> ...` 只进入输入框且 Agent 保持空闲；
- Esc 真实路径验证草稿不变；New UI 验证 grapheme 删除、选择移动、取消/接受、全屏覆盖和文本安全提示；
- New UI 子进程真实输入传统控制字节与 CSI-u `Ctrl+P`，并从协议日志证明选择和取消均未发送 `submit`；
- 权限与模型交互到来时覆盖层关闭，阻塞请求成为权威焦点；
- Ruff、compile、Node syntax 和相关 Python/Node 小模块测试通过；不运行全量测试。

## 当前不足与下一依赖

UI-14.2 仍为 partial。UI-14.2e 已按此前前置要求实现可取消、workspace 隔离、后台构建和有界内存的文件索引，
并接入两端文件 provider；UI-14.2f 继续复用当前 Agent Control schema v3 one-shot snapshot 接入 Agent provider。
页面 provider 应继续读取现有权威 snapshot/store，禁止复制第二套状态。
UI-14.2b 已在 `UI-14-2b-recent-command-ranking.md` 交付本次启动内的无敏感内容 MRU 合同；跨启动持久历史
仍不属于瞬态 QuickOpen 状态。typed argument form、Vim mode 和键冲突诊断仍属于后续切片。
