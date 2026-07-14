# ARC-01.3a SessionPort 设计

## 背景

ARC-01.1 已建立可重复生成的 Python import graph，ARC-01.2 已为现有模块建立唯一领域
owner。当前 `AgentEngine` 仍直接构造并调用 SQLite `SessionStore`，使 Runtime 同时承担：

- 决定使用哪个会话存储实现；
- 管理会话存储生命周期；
- 执行会话创建、读取、保存、归档和删除；
- 向旧 CLI、TUI、New UI 暴露具体存储对象。

ARC-01.3 必须逐个建立真实 Port。本切片只处理 `SessionPort`，不创建其他空 Protocol，
不移动目录，也不提前实现 composition root。

## 目标

1. 定义覆盖现有会话生命周期的强类型异步 `SessionPort`。
2. 让 `AgentEngine` 的全部内部会话持久化调用只经过注入的 Port。
3. 默认运行仍使用现有 SQLite `SessionStore`，行为、数据格式和关闭语义不变。
4. 保留 `engine.session_store` 只读兼容入口，旧 CLI/TUI/New UI 无需同步迁移。
5. 用一个真实流式任务证明“构造注入→创建→保存→加载→完成回执”链路成立。

## 非目标

- 不移动 `Session` 数据类或 `SessionStore` 文件。
- 不修改 SQLite schema、标题生成、历史清洗、删除对账或权限撤销语义。
- 不实现 ModelPort、ToolExecutionPort、EventSink 或 PermissionPort。
- 不把默认 `SessionStore` 的构造迁移到启动层；该工作属于 ARC-01.4。
- 不删除 `engine.session_store`；移除条件由 ARC-01.5 记录。
- 不批量修改 UI、CLI 或测试中的会话存储访问方式。

## 已审计的权威调用链

`AgentEngine` 当前有 9 个直接持久化调用点：

1. `__init__` 构造 `SessionStore`；
2. `shutdown` 关闭存储；
3. `get_or_create_session` 创建会话；
4. `load_session` 加载会话；
5. `list_sessions` 分页查询；
6. `_delete_session_and_reconcile` 删除会话；
7. 删除取消对账时再次加载会话；
8. `archive_session` 归档会话；
9. `_save_session` 保存完整历史。

真实流式链路为：

```text
run_streaming
  -> get_or_create_session
  -> _run_streaming_core
       -> get_or_create_session
       -> ReAct streaming
       -> _save_session
  -> ChatRunRecorder.finish
  -> completion_receipt
```

因此验收不能只检查 `isinstance` 或 import；必须运行这条链路并从注入 Port 重新加载已保存会话。

## 方案比较

### 方案 A：泛型结构化 Protocol + 构造注入（采用）

在 `runtime/ports/session.py` 定义 `SessionPort[TSession]`。Port 不导入
`memory.session.Session`，只约束存储操作；`AgentEngine` 在消费处将类型具体化为
`SessionPort[Session]`。

优点：

- Port 不依赖 SQLite adapter 或 Memory 领域实现；
- `SessionStore` 无需继承基类即可结构化满足契约；
- 测试可注入记录型、故障型或远端型实现；
- 不搬迁 `Session`，改动范围可控；
- 后续 composition root 可直接接管实例构造。

代价：

- Runtime 仍暂时认识 `Session` 数据实体；本切片只反转存储实现依赖，不重塑实体边界。
- Python 的运行时 Protocol 检查只验证成员存在，签名正确性仍需 contract tests 保证。

### 方案 B：抽象基类

要求所有存储实现继承 `SessionPortABC`。

不采用：它把继承关系强加给现有与第三方 adapter，替换成本高于结构化 Protocol，且没有带来
额外运行时安全。

### 方案 C：同时迁移 Session DTO 与 Store

将 `Session` 一并迁移到 core/runtime，再让 SQLite adapter 依赖新 DTO。

不采用：它把实体迁移、schema 兼容和 Port 注入揉成一个功能，违反“一次一个功能”，也会扩大
CLI、TUI、New UI 和测试的改动面。

## Port 契约

`SessionPort[TSession]` 必须声明以下异步方法，参数默认值与现有 `SessionStore` 保持一致：

```python
async def create_session(
    title: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> TSession

async def save(session: TSession) -> None
async def load(session_id: str) -> TSession | None
async def list_sessions(
    page: int = 1,
    page_size: int = 20,
    query: str = "",
) -> tuple[list[TSession], int]
async def delete(session_id: str) -> bool
async def archive(session_id: str) -> bool
async def close() -> None
```

契约规则：

- `create_session` 返回已经持久化、具有稳定 id 的会话；
- `save` 完成时数据必须可被同一 Port 的 `load` 读取；
- `load` 对不存在 id 返回 `None`，不把“未找到”当异常；
- `list_sessions` 保留分页、查询和总数语义；
- `delete`、`archive` 返回是否实际影响一条会话；
- `close` 可重复调用，关闭后允许实现按既有语义惰性重连；
- Port 不吞掉存储异常，Runtime 继续负责现有错误与取消对账。

## 注入与兼容设计

`AgentEngine.__init__` 增加仅限关键字参数：

```python
session_port: SessionPort[Session] | None = None
```

- 传入时，Engine 使用该实例；
- 未传入时，继续构造 `SessionStore(config.memory)`；
- Engine 在 `shutdown` 中关闭当前 Port，保持既有“Engine 拥有会话存储生命周期”的语义；
- Engine 内部只访问 `_session_port`；
- `session_store` 作为只读 property 返回 `_session_port`，为现有调用方保留兼容；
- 本切片不允许通过 `session_store` 替换 Port，替换必须在构造时显式完成。

只读兼容入口避免 `_session_port` 与 `session_store` 两份可变引用发生漂移。现有调用方对
`engine.session_store.load(...)`、方法打桩和状态检查仍然有效。

## 错误、取消与生命周期

- 注入对象缺少方法时，运行时 `isinstance(port, SessionPort)` 必须拒绝，并给出中文配置错误；
- 不在 Port adapter 中增加通用 `except Exception`，避免隐藏数据库或远端故障；
- 会话删除期间的取消仍由 `_delete_session_and_reconcile` 负责，Port 只提供权威删除/加载结果；
- `shutdown` 继续通过 `_shutdown_component` 隔离关闭失败，不能阻止其他组件释放；
- 同一个 Port 实例不得由多个 Engine 共享；当前生命周期契约明确由 Engine 独占并关闭。

## 测试策略

### Contract tests

使用真实临时 SQLite `SessionStore` 验证：

- 结构上满足 `SessionPort`；
- create/save/load 的数据闭环；
- list/query、archive、delete 和 close 语义；
- 不存在 id 返回 `None` / `False`。

### Engine 注入 tests

使用记录型 Port 包装真实 `SessionStore`：

- `AgentEngine` 暴露的 `session_store` 与注入对象是同一实例；
- Engine 内部创建、保存、加载、列表、归档、删除、关闭均命中注入 Port；
- 缺失契约成员的对象在构造时中文报错。

### 真实流式场景

仅替换模型网络流为确定性 token 流，其余走真实 Engine：

1. 从临时工作区和临时 SQLite 启动 Engine；
2. 注入记录型 SessionPort；
3. 调用 `run_streaming`；
4. 收到 `completion_receipt`；
5. 通过注入 Port 加载会话；
6. 断言用户消息、助手消息、workspace、token 与 receipt 均完整；
7. shutdown 后断言 Port 被关闭。

这不是 mock-only 测试：SQLite、Session 序列化、Engine 流式循环、ChatRunRecorder 和回执均真实执行。

## 验收标准

- `SessionPort` 没有 `Any` 返回值或自由 dict 契约；
- 默认 `SessionStore` 与记录型 adapter 均通过 contract tests；
- `AgentEngine` 源码中除默认 adapter 构造外，不再出现内部 `self.session_store.*` 调用；
- 一个真实流式任务通过注入 Port 完成且 receipt 行为不变；
- 旧 `engine.session_store` 读取、方法打桩和 CLI/TUI/New UI 调用保持兼容；
- SessionPort 聚焦测试、现有 SessionStore 集成测试、Ruff 与 import 编译通过；
- 不运行全量测试，不修改其他 Port，不移动目录。

## 后续退出条件

ARC-01.3a 完成后：

- ARC-01.3 仍标记“进行中”，直到五个 Port 全部独立验收；
- ARC-01.4 可在所有真实 Port 完成后，把默认 adapter 构造移到 composition root；
- ARC-01.5 在 UI/CLI 全部改用 Engine service 后，记录 `session_store` 兼容 property 的删除条件。
