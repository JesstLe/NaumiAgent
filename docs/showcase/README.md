# NaumiAgent 界面截图

本目录用于以图片形式介绍 NaumiAgent 的终端交互能力。

- `00-overview.png`：启动、工具执行、Todo 与 Runtime Inspector 总览。
- `01-welcome-logo.png`：基于当前工作区配置渲染的启动欢迎页与 ASCII Logo。
- `02-first-run-guide.png`：首次启动配置引导。凭据仅显示安全存储状态，不包含真实密钥。
- `03-command-guide.png`：命令 QuickOpen 与键盘导航。
- `04-tool-execution.png`：工具读取、构建命令与运行中状态。
- `05-todo-progress.png`：常驻 Todo 进度和当前执行项。
- `06-runtime-sidebar.png`：计划、工具、上下文、改动与测试侧边栏。
- `07-task-panel.png`：Todo、子智能体、后台任务与浏览器任务聚合面板。
- `08-completion-receipt.png`：完成回执、验证证据和 Git 状态。

除启动配置字段来自当前项目外，其余任务内容是安全演示数据。所有终端画面均通过
`frontend/terminal-ui/src/render.js` 的真实渲染器和状态结构生成，不代表一次真实业务任务的执行结果。

重新生成：

```powershell
node scripts/capture-terminal-showcase.mjs
& scripts/capture-terminal-showcase.ps1
```
