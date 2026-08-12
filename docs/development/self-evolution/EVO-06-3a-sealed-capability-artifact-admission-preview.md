# EVO-06.3a Sealed Capability Artifact 与 Sandbox 准入预检

## 目标

把 EVO-06.2c 的有效人工批准连接到一份真实、可审计的 Python Tool 实现源码，但严格停在注册之前：
系统封存工作区源码快照，静态核对 Tool 结构、API schema、顶层副作用、内置名称冲突和临时 namespace，
形成可动态撤销的 Sandbox admission preview。

本切片不 import 候选模块、不实例化候选类、不修改 `ToolRegistry`、不执行 fixture，也不授予 Shadow 或
执行权限。`preview_ready` 只表示实现制品具备进入下一阶段真实 Sandbox 验证的前置条件。

## 为什么不能直接复用现有 ToolRegistry

现有 `ToolRegistry.register()` 对同名 key 直接赋值，会静默覆盖已有 Tool；`get()` 还会把 `.` 或 `__`
namespace 剥离到最后一段。未经隔离地注册进化候选，既可能覆盖内置能力，也可能通过旧 alias 解析误命中。

因此临时名称采用：

```text
evolution_sandbox:<specification-id 前 12 位>:<declared-tool-name>
```

冒号 namespace 不触发现有 `.`/`__` 归一化。本阶段只预留并验证名称，不写入 Registry。

## 实现制品契约

`EvolutionCapabilityImplementationArtifact` 是 content-addressed、SQLite 持久化的不可变源码快照，绑定：

- 当前 Candidate ID；
- 完整 Specification ID 与 digest；
- 当前有效 approved Governance Decision ID 与 digest；
- 规范化工作区相对 `.py` 路径、UTF-8 源码正文与 SHA-256；
- 目标类名、规格声明 Tool 名、临时 Tool 名；
- parameters schema、permission specification、verification specification digest；
- 八项确定性机械检查及 `admission_ready`；
- 永久为 false 的 registration、Shadow、execute authority。

源码正文只保存在本地 Store 中，New UI public payload 不传输源码正文。

## 八项机械检查

1. `approved_source_binding`：Decision 当前有效、结果为 approved，且绑定当前完整 Specification digest；
2. `workspace_source`：相对路径、无 `..`、无 Windows/Unix 绝对路径、路径链不经过 symlink、文件为 UTF-8
   `.py` 且不超过 256 KiB；
3. `tool_subclass`：指定类真实出现在 AST 顶层并声明 `Tool` 基类；
4. `interface_match`：`name` 与 `parameters_schema` 必须是可静态求值 literal，并与 Specification 完全一致；
5. `entrypoint_shape`：name/description/schema 是 property，description 非空，execute 是 async；
6. `import_time_safety`：模块顶层只允许 import、定义、docstring 和 literal 常量，不允许调用或控制流；
7. `builtin_conflict`：声明名不能与当前 Registry exact name 或旧 `.`/`__` alias 冲突；
8. `temporary_namespace`：临时名称当前唯一，且不会落入旧 namespace alias 冲突。

任何检查失败都会持久化为 `blocked` preview，便于审阅真实失败原因；不会注册“部分通过”的候选。

## 动态撤权与持久一致性

- `inspect` 每次重新读取当前 Proposal、Specification、Governance Decision 和工作区源码摘要；
- 源码删除/变化、Specification/Decision 变化或治理撤权后，历史 Artifact 保留审计，但 View 变为
  `revoked`；
- Artifact ID 绑定完整 payload；Store 另存 canonical payload digest，读取时双重校验；
- `(workspace, specification, source_sha256)` 唯一，并发重复封存幂等；SQLite `BEGIN IMMEDIATE` 防止
  竞态产生分叉；
- bypass 不改变治理来源、检查结果或 false authority。

## 双通道与 UI

- Slash inspect：`/evolution capability-artifact <candidate-id>`；
- Slash create：`/evolution capability-artifact <candidate-id> <source.py> <ClassName>`；
- Agent Tool：`evolution_capability_artifact(action='inspect|create', ...)`；
- New UI：typed `evolution/review/request` 支持同一 action，Candidate detail 显示状态、临时名、八项检查
  及源码/治理 current 状态；
- Textual/fallback 复用同一 service 与 renderer，不维护第二套规则。

## 验收证据

- 真实五步 Specification + 人工 approved Decision + 工作区 Python 文件可形成 sealed preview；
- 内置 alias 冲突和 import-time 调用同时被独立阻断；
- Unix/Windows 绝对路径、父目录跳转、symlink、非 Python、超限与非 UTF-8 均 fail closed；
- 治理 revoked、源码变化/删除立即把 View 投影为 revoked；
- 并发重复 record 只形成一个 Artifact；持久 payload/digest 篡改读取失败；
- Slash、Agent Tool、New UI、Candidate detail、fallback 共享同一逻辑；
- Python 相关模块测试与 Node protocol/state/render 小模块测试通过。

## 自我审视与下一步

本切片验证的是“代码是什么、是否与批准规格静态一致、是否有资格进入隔离验证”，尚未证明代码行为正确。
AST 无法证明权限实际使用、结果 schema、错误契约、真实 fixture、性能 SLO 或恶意运行时行为。

下一最小切片 EVO-06.3b 必须在 ARC-04/Harness Sandbox 中从 sealed source snapshot materialize 候选，
逐项运行 Specification scenarios、验证结果/错误 schema 与 permission observations；全部通过后才可签发短期、
可撤销的临时 Registry lease。仍不得进入 Shadow 或 Limited Activation。
