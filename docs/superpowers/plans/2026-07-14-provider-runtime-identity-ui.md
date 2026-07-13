# Provider 运行身份 UI 实施计划

1. 先为 `ModelRouter.get_runtime_identity()` 编写失败测试，覆盖 catalog 与 legacy。
2. 实现不可变运行身份，并让 JSONL Bridge 输出安全字段。
3. 为新 UI 协议边界、欢迎页和底部状态栏编写失败测试，再实现中文展示。
4. 为 Textual TUI 编写真实 catalog 启动测试，再接入同一运行身份。
5. 仅运行相关 Python/Node 小模块测试、Ruff 与 compileall，完成自审后提交并合并到 `main`。
