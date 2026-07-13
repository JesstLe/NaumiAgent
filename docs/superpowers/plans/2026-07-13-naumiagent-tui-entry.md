# `naumiagent --tui` 实施计划

1. 在 `src/naumi_agent/main.py` 增加独立的 `naumiagent_cli` 入口，支持 `--tui`、`--config` 和无参数帮助，复用现有 `_launch_terminal_ui`。
2. 在 `pyproject.toml` 的 `[project.scripts]` 注册 `naumiagent`，保留现有 `naumi`。
3. 为入口参数分发、错误退出和脚本注册增加单元测试。
4. 更新 `scripts/windows/setup.ps1`，通过 uv editable tool 安装当前项目，并扩展脚本契约测试。
5. 运行入口相关测试、完整 pytest、Ruff 和现有 Node 测试。
6. 在独立 PowerShell 进程中确认 `naumiagent` 可解析，并通过调试日志确认 `naumiagent --tui` 完成前端/bridge 握手。
7. 扫描密钥、确认 `apps/macos` 零改动，提交实现且不推送远端。
