# ARC-01.3b PermissionPort 设计

## 背景

ARC-01.3a 已把会话持久化从 `AgentEngine` 的具体 `SessionStore` 依赖中解耦。当前 Engine
仍直接构造并调用 `PermissionChecker`，权限模式、次数重置和每次工具调用的判定都绑定在具体
实现上。

现有调用面共 8 处：

1. 构造 `PermissionChecker`；
2. 读取初始 mode；
3. `permission_mode` 状态读取；
4. default/plan/bypass 切换时 `set_mode`；
5. Engine reset 时 `reset_counts`；
6. 活跃会话删除后 `reset_counts`；
7. 工具执行前 `check`；
8. 权限确认 payload 读取 mode。

本切片建立真实 `PermissionPort`，重点保持已经确定的产品语义：`bypass` 是全权限放行，
不执行确认、路径、命令、工具风险和次数限制；`plan` 仍只允许只读工具；`default` 恢复启动时
的权限模式。

## 目标

1. 定义强类型、同步、可运行时检查的 `PermissionPort`。
2. 让 `AgentEngine` 内部所有权限策略访问只经过注入 Port。
3. 默认继续使用现有 `PermissionChecker`，规则表和判定行为不变。
4. 支持替换为记录型、远端或策略组合 Port，并可验证实际被调用。
5. 保持旧 `_permission_checker` 方法打桩兼容，但只保留一个权威对象。
6. 用真实临时目录与真实工具执行验证 default/plan/bypass，而非只检查枚举值。

## 非目标

- 不修改 `TOOL_PERMISSIONS`、`PREFIX_PERMISSIONS` 或危险命令规则。
- 不增加高风险二次确认；`requires_double_confirm` 继续为 false。
- 不改变 PermissionGrant、确认 UI、Hook 或 session transition 语义。
- 不实现 EventSink、ModelPort 或 ToolExecutionPort。
- 不移动 Safety 值对象、规则表或 `PermissionChecker` 文件。
- 不把默认 adapter 构造迁移到 composition root；该工作属于 ARC-01.4。

## 方案比较

### 方案 A：Runtime Protocol 复用 Safety 值对象（采用）

在 `runtime/ports/permission.py` 定义 `PermissionPort`，方法使用现有：

- `PermissionMode`；
- `PermissionDecision`；
- `PermissionAwareTool`。

Port 只依赖稳定值对象，不依赖 `PermissionChecker`、规则表或沙箱实现。现有 Checker 通过结构化
typing 自动满足契约。

优点：

- 不复制安全枚举和判定 DTO，避免两套真相；
- Engine 可注入其他策略实现；
- 改动只覆盖 8 个已审计调用点；
- 与 SessionPort “先反转实现、暂不搬实体”的渐进策略一致。

代价：Runtime Port 仍认识 Safety 的公共值对象。彻底移动公共 DTO 属于后续领域模型整理，不在
本切片扩张。

### 方案 B：泛型化 Mode/Decision/Tool

用三个 TypeVar 隔离全部 Safety 类型。

不采用：Engine 立即需要访问 `decision.outcome`、`risk_level`、`tool_family` 等字段，泛型并未消除
真实语义依赖，只会降低可读性和静态检查质量。

### 方案 C：Port 自己定义新 Permission DTO

不采用：需要双向转换，并可能让 bypass、确认、风险等级出现两套不一致语义。

## Port 契约

```python
@property
def mode() -> PermissionMode

def set_mode(mode: PermissionMode) -> None

def check(
    tool_name: str,
    args: Mapping[str, object],
    tool: PermissionAwareTool | None = None,
) -> PermissionDecision

def reset_counts() -> None
```

契约规则：

- `mode` 是当前判定使用的权威模式；
- `set_mode` 必须影响之后所有 `check`；
- `check` 不修改传入参数，返回完整 `PermissionDecision`；
- `reset_counts` 只清除会话级调用计数，不改变 mode 或规则；
- Port 不吞掉策略异常，Engine 继续按现有失败路径处理；
- Port 不负责用户确认，确认属于 Runtime/UI 交互层；
- Port 不负责持久化 grant，grant store 仍由 Engine 管理。

`args` 使用 `Mapping[str, object]`，因为权限判定只读取参数，不应要求可变 dict，也不应在 Port
中暴露自由 `Any`。现有 `PermissionChecker` 的公开 `check` 与内部 runtime MCP helper 同步收窄
为 Mapping，不改变运行行为。

## 注入与兼容

`AgentEngine.__init__` 增加 keyword-only 参数：

```python
permission_port: PermissionPort | None = None
```

- 未传入时构造现有 `PermissionChecker`；
- 显式传入时即使对象布尔值为 false 也必须使用它；
- 缺少任一契约成员时，在初始化其他服务前抛出中文 `TypeError`；
- 唯一权威字段为 `_permission_port`；
- Engine 内部不得再调用 `self._permission_checker.*`；
- `_permission_checker` 保留为只读 property，返回 `_permission_port`，兼容现有方法打桩；
- 注入 Port 的初始 `mode` 决定 Engine 的 default mode；bypass 初始模式映射为 Runtime BYPASS，
  其他模式映射为 Runtime DEFAULT。

PermissionPort 没有资源生命周期方法，不在 shutdown 中新增无意义 `close`。

## 模式不变量

### Default

- 恢复 Engine 启动时 Port 的 mode，而不是硬编码 moderate；
- 默认 Checker 仍从 `config.safety.permission_mode` 初始化；
- UI 状态与确认 payload 必须读取 Port 当前 mode。

### Plan

- Runtime mode 为 plan 时 Port mode 设为 strict；
- Engine 的只读 allowlist 仍是第一层门；
- 写工具不得触发真实写入；
- 只读工具继续经过 PermissionPort.check。

### Bypass

- Port mode 设为 bypass；
- `PermissionChecker.check` 立即返回 ALLOW；
- 不做路径沙箱、危险命令、未知工具、次数或 metadata 风险检查；
- 不触发确认回调和 permission grant；
- 显式预算、会话切换栅栏与操作系统错误仍然生效，因为它们不是权限确认。

## 测试策略

### Contract tests

- 默认 `PermissionChecker` 是 `PermissionPort`；
- 缺少 mode/set_mode/check/reset_counts 任一成员的对象被拒绝；
- Port 公开成员集合精确，不创建空接口集合；
- Mapping 参数不被 Checker 修改。

### Engine 注入 tests

记录型 Port 包装真实 `PermissionChecker`，验证：

- 构造注入和 `_permission_checker` 兼容 property 指向同一对象；
- falsey Port 不会被默认 Checker 替换；
- 无效 Port 中文失败且不创建运行数据；
- mode、set_mode、check、reset_counts 全部命中注入对象；
- reset 与活跃会话删除后的 count reset 语义不变。

### 真实模式场景

使用临时工作区和真实内置工具：

1. default/moderate 下，越界路径被 Port 拒绝；
2. plan 下，写文件被 Engine 只读门阻止，读文件成功；
3. bypass 下，删除临时测试目录的高风险命令真实执行成功；
4. bypass 执行过程不触发确认回调或 grant；
5. 切回 default 后，同类高风险命令再次被现有策略阻止/确认。

测试只操作 pytest 临时目录，不触碰仓库或用户文件。

## 架构产物

新增 `runtime.ports.permission` 后：

- 模块数预期从 326 增至 327；
- 新模块 owner 必须为 Runtime；
- import_time/typing/all_static SCC 数量不得增加；
- import graph baseline 与 ownership artifact 必须以最终源码 H1 SHA 为 `source_base`；
- 两次生成结果逐字节一致，两个 artifact 的 graph digest 必须互锁。

## 验收标准

- `PermissionPort` 是真实可替换接口，不包含默认策略或 Prompt；
- Engine 内部不存在 `self._permission_checker.*` 调用；
- 默认 Checker、注入记录型 Port 和 falsey Port 均通过契约；
- default/plan/bypass 真实工具场景通过，bypass 保持全权限放行；
- 高风险确认、grant、会话切换和预算相关既有聚焦测试通过；
- Ruff、compileall、架构聚焦测试和双扫描通过；
- 不运行全量测试，不修改其他 Port 或权限规则表。

## 后续

ARC-01.3b 完成后，ARC-01.3 仍保持“进行中”。剩余 Port 按调用面和产品收益继续逐个设计：

1. ModelPort；
2. ToolExecutionPort；
3. EventSink。

EventSink 的 71 个调用点不得与本切片合并实施。
