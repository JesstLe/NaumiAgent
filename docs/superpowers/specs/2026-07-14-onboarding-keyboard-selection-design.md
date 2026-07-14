# 初始化提供商键盘选择设计

日期：2026-07-14

## 1. 目标

首次运行 Naumi 选择模型提供商时，支持 `↑/↓` 移动、数字快捷键和 `Enter` 确认；同时保留非 TTY、重定向输入和不兼容终端的编号/名称输入降级，不能因为增强交互破坏脚本化安装。

## 2. 方案比较

### 方案 A：自行解析 ANSI 按键

需要分别处理 Windows 控制台、POSIX raw mode、方向键序列、终端恢复和宽字符渲染，容易留下终端状态。项目已依赖 Prompt Toolkit，不采用重复实现。

### 方案 B：Prompt Toolkit `radiolist_dialog`

具备方向键，但按钮焦点、空格选择和全屏对话框与“上下选择、Enter 确认”的初始化体验不完全一致。不采用。

### 方案 C：Prompt Toolkit `choice`

`choice` 是非全屏选择器，默认焦点就在选项列表，原生支持上下移动、数字定位和 Enter 确认；可显示底部帮助，并能通过 `create_app_session` 做真实按键测试。采用此方案。

## 3. 组件边界

新增通用 `ui/selection.py`：

- `TerminalChoice`：稳定 value、用户可见 label、可选 description；
- `select_terminal_choice()`：验证空列表、重复 value 和默认值；
- TTY 路径调用 Prompt Toolkit `choice`；
- 非 TTY 路径调用注入的 fallback，便于 Rich Prompt 和自动化测试复用；
- `KeyboardInterrupt` 保持中断语义，不静默选择默认项；
- `EOFError` 交给 fallback 或返回默认值的行为由调用方明确决定。

onboarding 只负责提供选项和 Rich 降级提示，不再自己实现交互状态。

## 4. 用户体验

TTY 中显示：

```text
选择模型提供商
  1. Kimi Coding API
> 2. OpenAI
  3. Anthropic
  4. 自定义 API
↑/↓ 选择 · 数字定位 · Enter 确认 · Ctrl+C 取消
```

默认项保持 Kimi。非 TTY 或 Prompt Toolkit 运行环境不可用时显示编号列表，接受：

- `1` 到 `4`；
- `kimi`、`openai`、`anthropic`、`custom`；
- 空输入选择默认项。

错误输入由 Rich Prompt 继续提示，不产生堆栈或英文内部错误。

## 5. 跨平台与版本

- Prompt Toolkit 负责 Windows Console 与 POSIX raw mode；
- `full_screen=False`，退出后不留下 alternate-screen 内容；
- 鼠标默认关闭，纯键盘可完成；
- `prompt-toolkit` 最低版本固定为提供 `choice` 的 `3.0.52`，与当前锁文件一致；
- stdin 或 stdout 非 TTY 时不尝试进入 raw mode。

## 6. 测试

只运行选择组件和 onboarding 单测：

- 真实 `create_pipe_input` 发送方向键 + Enter，确认选择发生变化；
- 数字快捷键 + Enter；
- 默认项；
- 非 TTY 回退；
- 重复 value、缺失默认值、空选项拒绝；
- `_choose_provider()` 的编号与名称降级；
- `ruff check`、`py_compile`、`git diff --check`。

## 7. 自我审视

- 仅把 Rich `choices` 改成数字仍不算键盘选择；必须验证真实方向键事件。
- 不能在非 TTY 强行运行 Prompt Toolkit，否则 CI、管道和 IDE 控制台可能挂起。
- Ctrl+C 应取消 onboarding，而不是意外采用默认提供商并继续索取密钥。
- 本轮只替换 provider 选择；权限模式后续可复用同一组件，但不与本功能捆绑，遵守独立交付。
