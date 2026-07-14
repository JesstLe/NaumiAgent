# ARC-01.2 Domain Ownership 设计

## 1. 目的与边界

ARC-01.2 为每个 `naumi_agent` Python 模块指定唯一领域 owner，使新增模块的职责归属可以被机器验证，
并为后续 Ports、Composition Root 与 Import Rules 提供稳定输入。

本切片只回答“谁对模块语义负责”，不回答“哪些 owner 可以依赖哪些 owner”。依赖方向、存量 debt
预算和 CI 阻断属于 ARC-01.6；目录移动属于端口迁移完成后的独立工作。本切片不得修改
`AgentEngine`、CLI/TUI/New UI 运行路径，也不得把 ownership 当作大规模搬目录的理由。

## 2. 方案选择

采用中央类型化规则表，而不是目录内分散 `OWNERS` 文件或业务模块内元数据：

- 规则由 frozen dataclass 和 enum 表达，静态可读、测试可构造，不依赖 YAML 或运行时配置。
- 每条规则只允许 `exact` 或 `prefix` 两种匹配，不使用正则和 catch-all。
- 默认规则按顶层模块或包划分，新增未知顶层目录必须显式选择 owner，不能被兜底规则吞掉。
- 扫描消费 ARC-01.1 的 `ImportGraphReport`，不得另写一套模块发现逻辑。
- ownership 报告是确定性数据；不读取或导入被扫描的应用模块。

## 3. 八个唯一 owner

| Owner | 对其负责的语义 | 明确不负责 |
| --- | --- | --- |
| `model` | 模型路由、provider 能力、上下文与输出限制、模型调用协议 | 工具执行、UI 渲染、会话持久化 |
| `runtime` | 进程入口、编排循环、Agent 生命周期、transport、配置装配、协议运行时 | 具体 Tool 语义、UI 状态、持久化 schema |
| `tools` | Tool 接口、工具实现、Skill/MCP 工具接入与工具发现 | Runtime 主循环、Permission 规则、UI 交互 |
| `memory` | 会话、run record、长期记忆与持久化语义 | 调度、渲染、工具权限 |
| `safety` | 权限、验证、隔离、worktree 安全边界 | 业务工具行为、模型路由、UI 布局 |
| `harness` | 执行证据、回执、检查器、调试轨迹与评测基础设施 | Session Store 复制、Runtime 权威、UI 状态 |
| `ui` | CLI/TUI/New UI、Workbench 展示、输入、剪贴板与用户交互 | Tool execute 语义、Permission 规则、模型调用 |
| `tasks` | 任务、调度、后台队列与工作单元生命周期 | Agent 推理循环、会话持久化、UI 渲染 |

Owner 表示语义维护责任，不等同于组织成员、GitHub CODEOWNERS 或允许依赖列表。

## 4. 默认模块归属

下表冻结当前顶层模块的 owner。包规则匹配自身及其所有子模块；单文件规则只做 exact 匹配。

| Owner | 顶层包或模块 |
| --- | --- |
| `model` | `model` |
| `runtime` | `naumi_agent` 根包、`__main__`、`main`、`runtime`、`orchestrator`、`streaming`、`agents`、`agent_control`、`api`、`config`、`hooks`、`architecture`、`release`、`deploy`、`packaging_entry`、`log_setup` |
| `tools` | `tools`、`skills`、`mcp` |
| `memory` | `memory`、`runs` |
| `safety` | `safety`、`validation`、`worktree` |
| `harness` | `harness`、`inspector`、`debug_trace` |
| `ui` | `ui`、`tui`、`cli`、`workbench`、`assets`、`clipboard`、`cli_completer`、`user_interaction` |
| `tasks` | `tasks`、`scheduler`、`background` |

不设置 `naumi_agent.* -> runtime` 的兜底规则。未来出现 `naumi_agent.evolution`、`protocol`、
`daemons` 等新顶层包时，coverage 必须失败并要求显式决策；这能防止架构继续无意识增长。

## 5. 类型与数据契约

生产文件为 `src/naumi_agent/architecture/ownership.py`，公开以下类型：

```python
class DomainOwner(StrEnum): ...
class OwnershipMatch(StrEnum): EXACT = "exact"; PREFIX = "prefix"

@dataclass(frozen=True, slots=True)
class DomainDefinition:
    owner: DomainOwner
    summary: str
    owns: tuple[str, ...]
    excludes: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class OwnershipRule:
    rule_id: str
    owner: DomainOwner
    match: OwnershipMatch
    module: str
    rationale: str

@dataclass(frozen=True, slots=True)
class OwnershipAssignment:
    module: str
    path: str
    owner: DomainOwner
    rule_id: str

@dataclass(frozen=True, slots=True)
class OwnershipIssue:
    module: str
    path: str
    code: str
    matching_rule_ids: tuple[str, ...]
    message: str

@dataclass(frozen=True, slots=True)
class DomainOwnershipReport:
    source_root: str
    source_base: str
    import_graph_digest: str
    assignments: tuple[OwnershipAssignment, ...]
    issues: tuple[OwnershipIssue, ...]
    summaries: tuple[OwnerSummary, ...]
    rules: tuple[OwnershipRule, ...]
    schema_version: int
    digest: str
```

核心 API：

```python
def analyze_domain_ownership(
    import_report: ImportGraphReport,
    *,
    source_base: str,
    rules: tuple[OwnershipRule, ...] = DEFAULT_OWNERSHIP_RULES,
) -> DomainOwnershipReport: ...

def require_complete_ownership(report: DomainOwnershipReport) -> None: ...
```

`analyze_domain_ownership()` 始终返回完整报告；未归属和多归属进入 `issues`。严格调用者使用
`require_complete_ownership()` 得到中文、逐模块的 `DomainOwnershipError`。报告和异常都不得用
`Any`、裸 dict 或绝对路径作为主契约。

## 6. 匹配与冲突规则

1. `exact` 仅匹配完整模块名。
2. `prefix` 匹配规则模块自身及 `rule.module + "."` 开头的子模块。
3. 一个模块匹配 0 条规则时产生 `unowned_module`。
4. 一个模块匹配 2 条或更多规则时产生 `ambiguous_owner`，即使规则 owner 相同也失败；规则重叠必须显式消除，不能依赖“更长前缀优先”。
5. `rule_id`、`module` 必须非空；同一 `rule_id` 不能重复；规则顺序不影响报告字节。
6. 每个 `DomainOwner` 必须有且只有一个 `DomainDefinition`，且 `summary/owns/excludes` 均非空。
7. assignment 数量加 issue 模块数量必须覆盖 import report 的全部模块；任何模块不能静默消失。

## 7. 确定性与 provenance

- 所有路径沿用 ARC-01.1 的仓库相对 POSIX 路径。
- assignments 按模块名、路径排序；issues 按模块名、code、rule id 排序；规则按 rule id 排序。
- canonical JSON 使用 UTF-8、`sort_keys=True`、紧凑分隔符且无时间戳。
- SHA-256 digest 由移除 `digest` 字段后的 canonical JSON 计算。
- `import_graph_digest` 绑定 ARC-01.1 的完整图报告。
- 正式 artifact 为 `docs/architecture/arc-01-domain-ownership.json`，包含完整 assignment，便于审查每个模块，而不是只保存计数。
- artifact 的 `source_base` 必须指向一个包含完全相同 `src/naumi_agent` 子树的提交；使用两阶段 amend 避免自引用。

## 8. CLI 与用户体验

入口：

```text
PYTHONPATH=src python3 -m naumi_agent.architecture.ownership \
  --source-root src/naumi_agent \
  --output docs/architecture/arc-01-domain-ownership.json \
  --source-base <commit>
```

CLI 复用 ARC-01.1 扫描器和原子 UTF-8 写入。成功时打印中文摘要：模块数、八个 owner 的计数、
未归属数和冲突数。存在 issue 时仍写出完整诊断 artifact，但退出码为 2，stderr 列出前若干问题
并提示检查输出文件；路径错误沿用 `ImportGraphScanError` 的中文信息。

## 9. TDD 与验收

定向测试文件：`tests/unit/test_architecture_ownership.py`。

必须按 RED→GREEN 覆盖：

1. exact/prefix 正向匹配和稳定排序。
2. 0 条规则产生 `unowned_module`，重叠规则产生 `ambiguous_owner`。
3. 重复 rule id、空字段、缺失/重复 DomainDefinition 被拒绝。
4. canonical JSON 与 digest 在规则输入顺序变化后逐字节一致。
5. 默认规则覆盖真实 `src/naumi_agent` 全部模块，assignment 数等于 import graph 模块数，issues 为 0，八个 owner 均非空。
6. CLI 真实子进程两次生成逐字节一致，中文摘要包含八个 owner，artifact 无绝对路径和时间戳。
7. 正式 artifact 与 CLI 重生成逐字节一致，`source_base` provenance 可由 Git 证明源码子树相同。

验证只运行新测试文件、ARC-01.1 导入图测试、变更 Python 的 Ruff、真实 CLI 与 `cmp`；不运行全量
pytest。完成后独立自审所有 issue 路径、规则重叠和新顶层包失败行为。

## 10. 非目标与后续

- 不生成或修改 GitHub `CODEOWNERS`。
- 不定义 owner 间允许依赖矩阵，不阻断现有反向依赖。
- 不创建 Ports、adapter 或 composition root。
- 不移动现有源码目录。
- ARC-01.3 以本报告中的 owner 边界定义 Port 消费者与提供者。
- ARC-01.6 消费 ARC-01.1 的 edge 与 ARC-01.2 的 assignment，计算跨 owner 违规和存量 debt。
