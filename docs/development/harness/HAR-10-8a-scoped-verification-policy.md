# HAR-10.8a Pursuit 定向验证策略

## 目标

Pursuit 是每轮都会评估、行动和验证的长周期循环。仓库级测试或 lint 如果被隐式放进 assessment、模型生成
action 或 success criterion，会在几十轮中反复执行，造成高负载、定位噪声和不可控等待。本切片把“每轮只跑
目标相关小模块”变成机械策略，而不是依赖 Prompt 自觉。

## 三层边界

### 1. Assessment 只采集轻量证据

每轮 assessment 允许：

- 最近 10 条有界 PursuitRun durable evidence；
- git diff/stat 的有界片段；
- 原始目标和成功标准明确提到的文件，各读取前 80 行；
- 上一轮最多 5 条 action result 摘要。

它不再自动执行 `pytest tests/` 或 `ruff check src/`。assessment 的职责是观察，而不是重复阶段门。

### 2. Success criterion 必须定向

Goal parser Prompt 明确要求 verification command 指向最小文件、test node 或 module。执行前的确定性策略继续
检查命令，Prompt 被忽略时仍 fail closed。

被禁止的典型命令包括：

- 无测试文件/test node 的 pytest、`unittest discover`；
- 不含明确源码文件的 `ruff check`；
- tox/nox；
- 无 `-- <test-file>` 的 npm/pnpm/yarn test；
- 无过滤器的 cargo test、仓库级 go test；
- 未指定测试目标的 make/just/task、Maven、Gradle、dotnet、Bazel；
- 未指定文件/过滤器的 Jest/Vitest/Mocha/AVA、RSpec、PHPUnit、Swift、Dart/Flutter、CTest；
- 通过 `sh/bash/zsh -c` 包装上述命令，或把它们放入 `&&`、`;`、pipe 的任一分段。

策略阻止后 criterion 进入 `failed`，evidence 给出中文范围原因，bash/background 工具调用次数为零。
下一轮 assessor 会同时看到 criterion 的上次 evidence，因此能改为定向命令，而不是只看到 `failed` 后重复
同一广域计划。

### 3. 模型生成 Action 使用同一策略

`_execute_via_bash()` 在决定同步或后台执行之前应用同一检查。模型无法通过把全量测试包装成普通 action
绕过 criterion 门；阻止结果作为 action error 进入下一轮证据。

## 允许的目标形式

- `pytest tests/unit/test_demo.py`
- `pytest tests/unit/test_demo.py::test_case`
- `ruff check src/naumi_agent/demo.py`
- `npm test -- tests/demo.test.ts`
- `cargo test test_parser`
- `go test ./internal/parser`
- Maven `-Dtest`、Gradle `--tests`、dotnet 项目/`--filter`、具体 Bazel target。
- 具体 JS/Ruby/PHP/Dart 测试文件，以及 Swift `--filter`、CTest `-R`。

非验证类普通命令不受本策略扩权或替代权限系统；它仍由 PermissionChecker、sandbox 和 Harness lease 管理。

## 全量阶段门

全量测试没有被删除。它只能由用户明确手动运行，或由发布候选、CI、HAR-08 Eval/阶段门执行，不能作为
Pursuit 每轮的隐式探针。当前没有在 Pursuit 内提供“自动全量”旁路，以避免模型自行声称获得授权。

## 验收证据

- assessment 真实执行命令从 5 条降为 3 条（diff、目标文件、criterion 文件），没有 pytest/ruff 全量命令；
- injected executor 与 worktree cwd 路径保持一致；
- Python、JS、Rust、Go、Java、.NET、Bazel 典型广域命令被机械拒绝；
- 对应的定向命令继续允许；
- shell 嵌套和复合命令中的广域测试不能绕过；
- criterion 阻止时 bash 不执行；模型 action 阻止时 bash/background 均不执行；
- Pursuit/checkpoint/lease 小模块和文档治理通过，不运行全量测试。

## 当前不足

这是负载与验证范围策略，不是通用 shell 安全解析器，也不替代权限审批。任意自定义脚本名可能在内部运行
广域测试，只有后续 Tool Manifest/Validation Profile 才能从声明的测试 target 构建完全结构化执行计划。
HAR-10.5 仍需为允许执行的 destructive action 添加 durable idempotency 和外部状态 reconcile。
