# ARC-01.3a SessionPort 实现计划

> 对应设计：[ARC-01-3a-session-port-design.md](ARC-01-3a-session-port-design.md)

## 交付边界

本计划只交付 `SessionPort`。采用 TDD，一次只推进一个可验证行为；所有生产改动最终形成一个
原子功能提交。设计和计划文档已分别独立提交，不与生产代码混合。

不运行全量测试。每一阶段只运行本阶段新增测试和受影响的现有小模块测试。

## 预期文件

### 新增

- `src/naumi_agent/runtime/ports/__init__.py`
- `src/naumi_agent/runtime/ports/session.py`
- `tests/unit/test_session_port.py`

### 修改

- `src/naumi_agent/orchestrator/engine.py`
- `docs/development/architecture/ARC-01-domain-boundaries.md`
- `docs/architecture/arc-01-import-graph-baseline.json`
- `docs/architecture/arc-01-domain-ownership.json`

除非 RED 测试证明结构契约不匹配，否则不修改 `memory/session.py`。不得修改 SQLite schema、
UI、CLI、TUI 或其他 Port。

## Task 1：建立基线并冻结现有行为

### 1.1 工作区隔离

从已同步的 `main` 创建 `codex/arc-01-3a-session-port` worktree。确认：

- 主工作区干净；
- `main == origin/main`；
- worktree 起点包含设计与本计划；
- 没有其他功能文件混入。

### 1.2 聚焦基线

运行：

```bash
PYTHONPATH=src python3 -m pytest \
  tests/integration/test_session_store.py \
  tests/unit/test_engine.py::TestRun::test_streaming_persists_authoritative_completion_receipt \
  tests/unit/test_engine.py::test_shutdown_continues_after_browser_cleanup_failure -q
```

记录通过数量和已有 warning。若基线失败，先判断是否与本功能无关，不得掩盖失败。

## Task 2：定义真实 SessionPort 契约

### 2.1 RED：结构契约测试

先新增 `tests/unit/test_session_port.py`，只写以下失败测试：

- `SessionStore` 是 `SessionPort`；
- 缺少任一方法的对象不是 `SessionPort`；
- 一个完整记录型 adapter 是 `SessionPort`；
- Port 的公开方法集合恰好覆盖 create/save/load/list/delete/archive/close。

运行：

```bash
PYTHONPATH=src python3 -m pytest tests/unit/test_session_port.py -q
```

预期因 `runtime.ports.session` 不存在而 RED。

### 2.2 GREEN：最小契约实现

新增：

- `runtime/ports/session.py`：泛型、`@runtime_checkable` 的 `SessionPort[TSession]`；
- `runtime/ports/__init__.py`：只导出 `SessionPort` 和类型变量需要的公开符号。

要求：

- 不导入 `SessionStore` 或 `memory.session`；
- 不使用 `Any` 返回值；
- 7 个方法参数、默认值、返回类型与设计一致；
- 只有接口，没有默认存储逻辑、Prompt 或空实现。

重跑 `tests/unit/test_session_port.py`，确认 GREEN。

### 2.3 REFACTOR

- 检查 Protocol docstring 是否说明创建即持久化、关闭由 Engine 管理；
- 检查 `__all__`；
- Ruff 只检查两个新生产文件和测试文件。

## Task 3：把 AgentEngine 内部调用迁移到 Port

### 3.1 RED：构造注入和兼容测试

在 `test_session_port.py` 增加失败测试：

- `AgentEngine(config, session_port=recording_port)` 接受完整 Port；
- `engine.session_store is recording_port`，兼容读取保持成立；
- 缺少方法的对象在构造阶段抛出中文 `TypeError`；
- 默认未注入时 `engine.session_store` 仍是 `SessionStore`；
- shutdown 通过注入 Port 调用一次 `close`。

这些测试应先因构造函数不接受 `session_port` 而 RED。

### 3.2 GREEN：注入和单一权威引用

修改 `AgentEngine`：

1. 构造函数增加 keyword-only `session_port`；
2. 选择 `session_port` 或默认 `SessionStore(config.memory)`；
3. 用 `isinstance(candidate, SessionPort)` 做结构校验；
4. 保存到唯一内部字段 `_session_port`；
5. 提供只读 `session_store` property 返回 `_session_port`；
6. shutdown 改为关闭 `_session_port`。

错误文案使用中文，并明确列出需要实现的契约，不输出内部 traceback 给终端用户。

### 3.3 RED/GREEN：逐项迁移 7 类操作

按以下顺序逐个写调用记录断言，再替换对应内部访问：

1. create：`get_or_create_session`；
2. load：`load_session`；
3. list：`list_sessions`；
4. archive：`archive_session`；
5. save：`_save_session`；
6. delete：`_delete_session_and_reconcile`；
7. delete 取消对账 load；
8. close：`shutdown`。

完成后执行静态断言：

```bash
rg -n 'self\.session_store\.' src/naumi_agent/orchestrator/engine.py
```

预期无结果。属性定义和外部兼容调用不计入内部直连。

## Task 4：真实流式端到端验收

### 4.1 RED：注入 Port 的流式场景

在 `test_session_port.py` 增加真实异步测试：

- 临时工作区；
- 临时 SQLite `SessionStore` 作为记录型 Port 的 delegate；
- 只把模型网络流替换为固定 `StreamChunk`；
- 调用真实 `AgentEngine.run_streaming`；
- 收集真实事件并等待完成。

断言：

- 结果为 completed，response 与 token 流一致；
- 首事件是 `run_started`；
- 恰好一个 `completion_receipt`，且等于 `result.receipt`；
- Port 至少记录 create、save、load；
- 通过 Port load 得到的会话包含 system/user/assistant 历史；
- workspace、token 和费用元数据已经保存；
- shutdown 调用 close。

### 4.2 GREEN/REFACTOR

只修复 Port 路由导致的问题，不改流式业务语义。若测试暴露现有独立缺陷，诚实记录并另开切片，
不得顺手扩张当前功能。

## Task 5：回归、架构产物与文档状态

### 5.1 聚焦回归

运行：

```bash
PYTHONPATH=src python3 -m pytest \
  tests/unit/test_session_port.py \
  tests/integration/test_session_store.py \
  tests/unit/test_engine.py::TestRun::test_streaming_persists_authoritative_completion_receipt \
  tests/unit/test_engine.py::test_shutdown_continues_after_browser_cleanup_failure -q
```

再运行 import graph 与 ownership 的 40 个聚焦测试，确保新增模块自动归入 Runtime 且没有新增
歧义。

### 5.2 正式基线

采用两阶段提交：

1. 先形成只含最终源码/测试/状态文档的 H1；
2. 以 H1 SHA 作为 `source_base`，重新生成 import graph baseline 和 ownership artifact；
3. 两次独立生成并逐字节比较；
4. 确认 graph digest 与 ownership `import_graph_digest` 相同；
5. amend 为最终 H2，并证明 `git diff H1 H2 -- src/naumi_agent` 为空。

预期模块数增加 2（`runtime.ports` 与 `runtime.ports.session`），owner 均为 Runtime。循环数量不得
增加；若增加则不得提交。

### 5.3 状态文档

将 ARC-01 状态表更新为：

- ARC-01.3 进行中；
- SessionPort 已实现；
- 其余四个 Port 待开发；
- 链接设计、实现计划、生产文件和测试。

## Task 6：提交前自审与合并

### 6.1 自审问题

- Engine 是否真的可以使用非 SQLite Port，而不只是类型标注？
- 是否仍有内部调用绕过 `_session_port`？
- 缺失方法、存储错误、删除取消和重复关闭是否保持明确语义？
- 旧 UI/CLI 的 `session_store` 读取是否仍有效？
- 真实流式回执是否与迁移前一致？
- 是否误改了其他 Port、schema、UI 或模型逻辑？

### 6.2 最终小范围验证

- 上述聚焦 pytest；
- Ruff 检查所有改动的 Python 文件；
- compileall 检查 `runtime/ports`、`engine.py`；
- import graph/ownership 各生成两次并比较；
- `git diff --check`；
- `git status --short` 必须为空。

已知本机可能出现 RequestsDependencyWarning。只要测试通过且 warning 与本改动无关，可以记录，
不得把它描述为本功能失败。

### 6.3 集成

1. 英文提交：`refactor(runtime): inject session persistence port [ARC-01.3a]`；
2. 确认远端 `main` 无新提交；
3. `--ff-only` 合并到 `main`；
4. 在 `main` 重跑本计划的小范围验证；
5. push 并用 `ls-remote` 校验 SHA；
6. 删除临时 worktree 和功能分支。

## 完成定义

只有以下条件全部满足才可声明 ARC-01.3a 完成：

- 生产代码存在可替换、强类型的 SessionPort；
- SQLite 默认实现和注入实现都通过同一契约；
- Engine 全部内部会话存储操作经过 Port；
- 一个真实流式任务通过注入 Port 完成并持久化 receipt；
- 兼容入口存在且默认用户行为不变；
- 架构产物与源码 SHA 互锁、可重复生成；
- 小范围验证通过并已推送 `main`；
- 清楚声明 ARC-01.3 其余四个 Port 仍未完成。
