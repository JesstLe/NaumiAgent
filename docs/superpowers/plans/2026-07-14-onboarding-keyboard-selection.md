# 初始化提供商键盘选择实施计划

> 对应设计：`docs/superpowers/specs/2026-07-14-onboarding-keyboard-selection-design.md`

## 任务 1：选择组件失败测试

1. 新建 `tests/unit/test_terminal_selection.py`。
2. 用 Prompt Toolkit pipe input 验证 `↓ + Enter` 和数字快捷键。
3. 验证非 TTY fallback 与非法选项定义。
4. 运行该文件，确认缺少实现而失败。

## 任务 2：实现通用选择组件

1. 新建 `src/naumi_agent/ui/selection.py`。
2. 实现选项验证、TTY 判定、`choice` 调用和 fallback。
3. 将最低 Prompt Toolkit 版本调整为 `3.0.52`。
4. 运行任务 1 测试直到通过。

## 任务 3：接入 onboarding

1. 在 `tests/unit/test_onboarding.py` 增加 TTY 路由与编号/名称 fallback 测试。
2. 修改 `_choose_provider()` 使用通用选择器。
3. 保留 Kimi 默认项和中文帮助。
4. 运行 onboarding 定向测试。

## 任务 4：验证与提交

1. 运行 `test_terminal_selection.py` 和 onboarding 相关测试。
2. 对修改 Python 文件执行 Ruff 与 `py_compile`。
3. 执行 `git diff --check` 并审查跨平台、中断和 EOF 路径。
4. 以英文独立提交。
