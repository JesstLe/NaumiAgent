# ARC-01.3b PermissionPort 实现计划

> 对应设计：[ARC-01-3b-permission-port-design.md](ARC-01-3b-permission-port-design.md)

## 交付边界

只实现 PermissionPort，不修改权限规则表，不创建其他 Port，不运行全量测试。生产代码、测试和
状态文档最终形成一个原子功能提交；架构产物使用两阶段提交与源码 SHA 互锁。

## 预期文件

### 新增

- `src/naumi_agent/runtime/ports/permission.py`
- `tests/unit/test_permission_port.py`

### 修改

- `src/naumi_agent/runtime/ports/__init__.py`
- `src/naumi_agent/orchestrator/engine.py`
- `src/naumi_agent/safety/permissions.py`
- `docs/development/architecture/ARC-01-domain-boundaries.md`
- `docs/architecture/arc-01-import-graph-baseline.json`
- `docs/architecture/arc-01-domain-ownership.json`

不得修改 `TOOL_PERMISSIONS`、`PREFIX_PERMISSIONS`、PermissionGrant、UI 确认协议或工具实现。

## Task 1：隔离与行为基线

### 1.1 Worktree

从已同步的 `main` 创建：

- branch：`codex/arc-01-3b-permission-port`
- worktree：`/Users/lv/Workspace/NaumiAgent-worktrees/arc-01-3b-permission-port`

确认 main、origin/main 和 worktree 起点一致。

### 1.2 小范围基线

运行 PermissionChecker 模块以及 Engine 的 6 个关键节点：

```bash
PYTHONPATH=src python3 -m pytest \
  tests/unit/test_permissions.py \
  tests/unit/test_engine.py::TestToolExecution::test_runtime_mode_cycle_updates_permission_mode \
  tests/unit/test_engine.py::TestToolExecution::test_plan_runtime_mode_blocks_write_tools \
  tests/unit/test_engine.py::TestToolExecution::test_plan_runtime_mode_allows_read_only_tools \
  tests/unit/test_engine.py::TestToolExecution::test_high_risk_tool_in_bypass_skips_confirmation \
  tests/unit/test_engine.py::TestToolExecution::test_bypass_mode_runs_dangerous_shell_command_without_confirmation \
  tests/unit/test_engine.py::TestSessionAuthorizationGeneration::test_engine_uses_permission_outcome_when_compatibility_booleans_disagree \
  -q
```

若节点所属类与源码不一致，先修正文档并记录 RED，不把“未收集测试”当通过。

## Task 2：定义 PermissionPort

### 2.1 RED：结构契约

先创建 `tests/unit/test_permission_port.py`，验证：

- `PermissionChecker` 满足 `PermissionPort`；
- 缺少 mode、set_mode、check 或 reset_counts 的对象被拒绝；
- Protocol 公开操作集合精确；
- `check` 接受只读 Mapping 且不修改参数。

先运行测试，预期因模块不存在而 RED。

### 2.2 GREEN：Protocol

新增 `runtime/ports/permission.py`：

- `@runtime_checkable` Protocol；
- `mode` property；
- `set_mode`、`check`、`reset_counts`；
- 使用 `Mapping[str, object]`；
- 返回现有 `PermissionDecision`；
- 不导入 `PermissionChecker` 或规则表；
- 不包含默认实现、Prompt、pass 或自由 `Any`。

更新 `runtime/ports/__init__.py` 导出 PermissionPort。

将 `PermissionChecker.check` 与 `_check_runtime_mcp_command` 的 args 注解收窄为 Mapping；不得改变
函数体判定顺序或规则内容。

重跑新测试和 Ruff，确认 GREEN。

## Task 3：Engine 构造注入与兼容

### 3.1 RED：注入行为

新增记录型 Port，包装真实 PermissionChecker，先写失败测试：

- `AgentEngine(..., permission_port=port)` 接受完整 Port；
- `engine._permission_checker is port` 兼容打桩仍成立；
- falsey Port 不被默认 Checker 替换；
- 无效 Port 中文 TypeError，且失败前不创建 `.naumi` 数据；
- 未注入时默认对象仍是 PermissionChecker。

### 3.2 GREEN：单一权威引用

修改 Engine：

1. 构造函数增加 keyword-only `permission_port`；
2. workspace 路径解析后立即解析默认/注入 Port；
3. 使用 `is None`，不得用布尔短路；
4. 结构校验失败时列出四个成员；
5. 唯一字段 `_permission_port`；
6. `_permission_checker` 改为只读兼容 property；
7. 初始 default/runtime mode 从 Port.mode 读取。

静态检查：

```bash
rg -n 'self\._permission_checker\.' src/naumi_agent/orchestrator/engine.py
```

预期无结果。

### 3.3 RED/GREEN：逐项迁移

逐项增加记录断言后迁移：

1. `permission_mode` → mode；
2. `set_runtime_mode` → set_mode；
3. `reset` → reset_counts；
4. 活跃会话删除 → reset_counts；
5. `_execute_tool` → check；
6. 确认 payload → mode。

兼容 property 只允许外部读取/方法打桩，Engine 内部不得绕回。

## Task 4：真实模式验收

使用记录型 Port + 真实 Checker + 真实内置工具，所有写操作局限在 pytest 临时目录。

### 4.1 Default

- 初始 moderate；
- 越界 `file_read` 被拒绝；
- bash_run 需要确认；
- Port 记录 check(mode=moderate)。

### 4.2 Plan

- `set_runtime_mode(plan)` 调用 Port.set_mode(strict)；
- file_write 不创建目标文件；
- file_read 成功并经过 Port.check；
- UI 可读状态为 strict。

### 4.3 Bypass

- `set_runtime_mode(bypass)` 调用 Port.set_mode(bypass)；
- 对临时目录执行真实 `rm -rf` 成功；
- 不触发 confirmer；
- 不创建 permission grant；
- 越界路径、未知工具和 destructive metadata 的 Checker 聚焦测试继续通过。

### 4.4 回到 Default

- 从 bypass 切回 default 恢复启动 Port 的 mode；
- 同类高风险操作重新进入阻止/确认语义；
- reset 清除计数但不改变 mode。

## Task 5：跨表面与安全回归

只跑相关节点：

```bash
PYTHONPATH=src python3 -m pytest \
  tests/unit/test_permission_port.py \
  tests/unit/test_permissions.py \
  tests/unit/test_engine.py::TestToolExecution::test_runtime_mode_cycle_updates_permission_mode \
  tests/unit/test_engine.py::TestToolExecution::test_plan_runtime_mode_blocks_write_tools \
  tests/unit/test_engine.py::TestToolExecution::test_plan_runtime_mode_allows_read_only_tools \
  tests/unit/test_engine.py::TestToolExecution::test_high_risk_tool_in_bypass_skips_confirmation \
  tests/unit/test_engine.py::TestToolExecution::test_bypass_mode_runs_dangerous_shell_command_without_confirmation \
  tests/unit/test_ui_bridge.py::test_bridge_bypass_response_switches_mode_and_allows_current_request \
  tests/unit/test_tui_agent_control.py::test_textual_bypass_confirmation_enables_full_permission_mode \
  -q
```

UI/TUI 测试只验证 Port 迁移没有改变现有交互，不在本切片修改界面。

## Task 6：架构产物

### 6.1 架构聚焦测试

运行 import graph + ownership 的 40 个测试。真实扫描必须得到：

- 327 个模块；
- `naumi_agent.runtime.ports.permission` owner=runtime；
- 0 ownership issues；
- SCC 数量不增加。

### 6.2 两阶段提交

1. 提交最终源码/测试/状态文档为 H1；
2. 用 H1 SHA 两次生成 import graph baseline 和 ownership artifact；
3. 两次结果逐字节一致；
4. graph digest 互锁；
5. artifact 无绝对路径和时间戳；
6. amend 为 H2；
7. 证明 `git diff H1 H2 -- src/naumi_agent` 为空。

## Task 7：最终自审、合并与推送

### 7.1 自审

- Port 是否真的可注入，还是只加了类型？
- bypass 是否仍在任何检查前立即 ALLOW？
- plan/default 是否恢复正确 mode？
- 是否仍有 Engine 内部 `_permission_checker.*` 绕行？
- 是否误改规则表、确认 UI 或其他 Port？
- 错误路径、falsey 实现、reset 和真实工具是否覆盖？

### 7.2 小范围验证

- 新 Port + PermissionChecker + 上述 Engine/UI/TUI 节点；
- Ruff 所有改动 Python 文件；
- compileall `runtime/ports`、`permissions.py`、`engine.py`；
- 40 个架构测试；
- 双扫描和 artifact cmp；
- `git diff --check`；
- clean status。

本机 Requests/Chroma deprecation warning 若与基线一致可记录，不得冒充功能失败。

### 7.3 集成

1. 英文提交：`refactor(runtime): inject permission policy port [ARC-01.3b]`；
2. fetch 并确认 main == origin/main；
3. `--ff-only` 合并；
4. main 上复跑小范围权限验收和 artifact cmp；
5. push 后 `ls-remote` 核对 SHA；
6. 删除 worktree 和功能分支。

## 完成定义

只有以下全部成立才可声明 ARC-01.3b 完成：

- PermissionPort 是可替换、强类型的真实边界；
- Engine 全部内部权限操作经过 Port；
- default/plan/bypass 真实工具行为无回归；
- bypass 明确保持全权限、无确认、无二次确认；
- 旧 `_permission_checker` 打桩与 UI/TUI 交互兼容；
- 聚焦测试、静态检查、架构产物与远端 SHA 均通过；
- ARC-01.3 仍诚实标明 ModelPort、ToolExecutionPort、EventSink 未完成。
