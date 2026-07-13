# `naumiagent --tui` 入口设计

## 目标

在 Windows PowerShell 中直接执行 `naumiagent --tui`，启动新版 Node 终端 UI，且不破坏现有 `naumi` 命令或 Mac/Swift App。

## 命令行为

- `naumiagent --tui` 启动新版 Node 终端 UI，行为等同于 `naumi ui`。
- `naumiagent --tui --config <path>` 将配置路径传给新版终端 UI。
- `naumiagent` 未携带 `--tui` 时显示帮助并正常退出，不隐式进入其他界面。
- 原有 `naumi chat`、`naumi ui`、`naumi run`、`naumi serve` 保持不变。

## 实现边界

1. 在 Python CLI 中增加一个独立、可单测的兼容入口，只解析 `--tui` 与 `--config`。
2. 在 `pyproject.toml` 中注册 `naumiagent` 控制台脚本，同时保留 `naumi`。
3. Windows 初始化脚本在完成依赖同步后，以 editable tool 方式安装当前项目，使 `naumiagent` 出现在 uv 的用户命令目录中。
4. 不修改 `apps/macos`、Swift 源码或 Mac App 的启动路径。

## 错误处理

- Node.js 缺失或版本过低时，复用现有终端 UI 的可读错误信息并返回非零退出码。
- 配置文件不存在时，沿用现有配置路径解析规则。
- 全局入口安装失败时，Windows 初始化脚本立即失败并显示不含密钥的错误。

## 验证

- 单元测试覆盖入口注册、`--tui` 分发、配置参数传递和无参数帮助。
- Windows 初始化脚本静态测试覆盖 editable tool 安装命令。
- 运行完整 Python 测试与 Ruff。
- 从独立 PowerShell 进程解析 `naumiagent`，启动 UI，并确认 Node 前端与 Python bridge 完成握手。
- 确认 `apps/macos` 相对上游无改动，仓库中无密钥。
