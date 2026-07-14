# ARC-01.2 Domain Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Do not dispatch subagents for this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为当前及未来所有 `naumi_agent` Python 模块提供唯一、类型化、确定性且可机器验证的八领域 owner。

**Architecture:** 新 ownership 模块只消费 ARC-01.1 `ImportGraphReport`，以无重叠 exact/prefix 规则生成 assignment、issue、owner summary 和 canonical artifact。CLI 复用同一分析 API，不自行发现模块；依赖方向与 CI 阻断留给 ARC-01.6。

**Tech Stack:** Python 3.13+ standard library、frozen/slotted dataclass、`StrEnum`、SHA-256、pytest、Ruff、Git provenance。

## Global Constraints

- 只实现 ARC-01.2，不实现 Ports、Composition Root、Legacy Adapter 或 Import Rules。
- 不修改 `AgentEngine`、CLI/TUI/New UI 运行路径，不移动现有源码目录。
- 所有用户可见错误与 CLI 摘要使用中文；代码注释与 commit message 使用英文。
- 不以 `Any`、裸 dict、YAML 或运行时全局配置作为主契约。
- 一个模块必须匹配恰好一条规则；0 条和 2 条以上均为失败。
- 默认规则没有 catch-all；新增顶层包必须显式归属。
- 只运行 ownership、import graph 和变更文件 Ruff 等定向验证，不运行全量 pytest。

---

### Task 1: 类型化领域定义与规则校验

**Files:**
- Create: `src/naumi_agent/architecture/ownership.py`
- Create: `tests/unit/test_architecture_ownership.py`

**Interfaces:**
- Consumes: `naumi_agent.architecture.import_graph.ModuleRecord`
- Produces: `DomainOwner`、`OwnershipMatch`、`DomainDefinition`、`OwnershipRule`、`validate_ownership_contract()`

- [ ] **Step 1: 写失败测试**

测试构造八个 `DomainDefinition`，并断言：owner 不重复、字段非空、rule id 不重复、module 非空；
重叠规则在分析阶段必须形成冲突，不能用最长前缀静默覆盖。

```python
def test_contract_rejects_duplicate_rule_ids() -> None:
    rules = (
        OwnershipRule("same", DomainOwner.RUNTIME, OwnershipMatch.PREFIX, "demo", "runtime"),
        OwnershipRule("same", DomainOwner.UI, OwnershipMatch.PREFIX, "demo.ui", "ui"),
    )
    with pytest.raises(DomainOwnershipError, match="rule_id"):
        validate_ownership_contract(DOMAIN_DEFINITIONS, rules)
```

- [ ] **Step 2: 运行 RED**

Run:

```text
PYTHONPATH=src python3 -m pytest -p no:cacheprovider tests/unit/test_architecture_ownership.py -q
```

Expected: collection fails because `naumi_agent.architecture.ownership` does not exist.

- [ ] **Step 3: 实现最小类型契约**

实现八值 `DomainOwner`、两值 `OwnershipMatch`、冻结 dataclass 和中文
`DomainOwnershipError`。`validate_ownership_contract()` 返回稳定排序后的 definitions/rules，拒绝重复、空字段、缺失 owner 和重复 owner。

- [ ] **Step 4: 运行 GREEN 与 Ruff**

```text
PYTHONPATH=src python3 -m pytest -p no:cacheprovider tests/unit/test_architecture_ownership.py -q
python3 -m ruff check src/naumi_agent/architecture/ownership.py tests/unit/test_architecture_ownership.py
```

Expected: contract tests pass; Ruff reports `All checks passed!`.

---

### Task 2: 唯一 assignment、issue 与 canonical report

**Files:**
- Modify: `src/naumi_agent/architecture/ownership.py`
- Modify: `tests/unit/test_architecture_ownership.py`

**Interfaces:**
- Consumes: `ImportGraphReport.modules`、`ImportGraphReport.digest`
- Produces: `OwnershipAssignment`、`OwnershipIssue`、`OwnerSummary`、`DomainOwnershipReport`、`analyze_domain_ownership()`、`require_complete_ownership()`

- [ ] **Step 1: 写 exact/prefix、未归属与冲突 RED**

```python
def test_analysis_reports_unowned_and_ambiguous_modules() -> None:
    report = _import_report("demo", "demo.ui", "demo.unknown")
    rules = (
        OwnershipRule("root", DomainOwner.RUNTIME, OwnershipMatch.EXACT, "demo", "root"),
        OwnershipRule("ui-a", DomainOwner.UI, OwnershipMatch.PREFIX, "demo.ui", "ui"),
        OwnershipRule("ui-b", DomainOwner.RUNTIME, OwnershipMatch.PREFIX, "demo.ui", "conflict"),
    )
    result = analyze_domain_ownership(report, source_base="base", rules=rules)
    assert [(issue.module, issue.code) for issue in result.issues] == [
        ("demo.ui", "ambiguous_owner"),
        ("demo.unknown", "unowned_module"),
    ]
```

- [ ] **Step 2: 写 canonical digest RED**

相同规则以不同输入顺序分析，断言 `canonical_json()` 和 `digest` 逐字节一致；移除 JSON
中的 `digest` 后重新计算 SHA-256 必须相同；JSON 不含时间戳和绝对路径。

- [ ] **Step 3: 实现确定性分析**

规则匹配只使用：

```python
def matches(rule: OwnershipRule, module: str) -> bool:
    if rule.match is OwnershipMatch.EXACT:
        return module == rule.module
    return module == rule.module or module.startswith(f"{rule.module}.")
```

每个模块匹配 1 条生成 assignment；否则生成 issue。summary 为八 owner 全量计数，包括 0。
报告按稳定键排序，digest 排除自身字段。

- [ ] **Step 4: 实现严格门并验证**

`require_complete_ownership()` 在 issues 非空时抛出中文错误，包含 issue 总数、前 10 个模块和
对应 rule id；无 issue 时无返回值。

```text
PYTHONPATH=src python3 -m pytest -p no:cacheprovider tests/unit/test_architecture_ownership.py -q
```

Expected: assignment、issue、digest tests pass.

---

### Task 3: 默认八领域 policy 与真实仓库覆盖

**Files:**
- Modify: `src/naumi_agent/architecture/ownership.py`
- Modify: `tests/unit/test_architecture_ownership.py`

**Interfaces:**
- Produces: `DOMAIN_DEFINITIONS`、`DEFAULT_OWNERSHIP_RULES`
- Consumes: Task 2 `analyze_domain_ownership()`

- [ ] **Step 1: 写真实仓库 RED**

```python
def test_default_policy_owns_every_real_naumi_module_exactly_once() -> None:
    root = Path(__file__).resolve().parents[2]
    graph = scan_import_graph(root / "src/naumi_agent", repository_root=root)
    report = analyze_domain_ownership(graph, source_base="test")
    assert len(report.assignments) == len(graph.modules)
    assert report.issues == ()
    assert {summary.owner for summary in report.summaries if summary.module_count} == set(DomainOwner)
```

- [ ] **Step 2: 实现 39 个顶层归属规则**

严格复制设计文档第 4 节映射。包使用 prefix；`naumi_agent` 根包和顶层 `.py` 使用 exact。
每条 rule id 使用 `<owner>-<module-slug>`，rationale 说明语义责任，不写依赖许可。

- [ ] **Step 3: 新顶层包 fail-closed 测试**

在合成 import report 增加 `naumi_agent.future_unknown`，断言 `unowned_module`；不得添加
`naumi_agent.*` fallback 让测试通过。

- [ ] **Step 4: 定向验证**

```text
PYTHONPATH=src python3 -m pytest -p no:cacheprovider \
  tests/unit/test_architecture_ownership.py \
  tests/unit/test_architecture_import_graph.py -q
```

Expected: both architecture test files pass.

---

### Task 4: CLI 与正式 ownership artifact

**Files:**
- Modify: `src/naumi_agent/architecture/ownership.py`
- Modify: `tests/unit/test_architecture_ownership.py`
- Create: `docs/architecture/arc-01-domain-ownership.json`

**Interfaces:**
- CLI: `python3 -m naumi_agent.architecture.ownership --source-root ... --output ... --source-base ...`
- Artifact: full assignments + rules + summaries + import graph digest + report digest

- [ ] **Step 1: 写 CLI RED**

真实 subprocess 扫描仓库，断言 exit 0、中文摘要包含八 owner、输出 JSON 可解析、issues 为空、
assignments 数等于真实 modules。第二次生成必须通过 `cmp`。

- [ ] **Step 2: 实现 CLI**

复用 `scan_import_graph()`、`analyze_domain_ownership()` 和 ARC-01.1 的原子 UTF-8 写入实现；
若 ownership issues 非空，先写 artifact 再返回 2，并在 stderr 输出中文诊断。

- [ ] **Step 3: 生成正式 artifact**

第一次生成使用当前工作树只验证内容，不提交伪 provenance。源码/测试稳定后先创建中间提交 H1，
再以 H1 作为 `--source-base` 重生成 artifact，确认：

```text
git diff H1 HEAD -- src/naumi_agent
```

在最终 amend 前输出为空。

- [ ] **Step 4: 真实验证**

```text
PYTHONPATH=src python3 -m naumi_agent.architecture.ownership \
  --source-root src/naumi_agent \
  --output /tmp/ownership-a.json \
  --source-base H1
PYTHONPATH=src python3 -m naumi_agent.architecture.ownership \
  --source-root src/naumi_agent \
  --output /tmp/ownership-b.json \
  --source-base H1
cmp /tmp/ownership-a.json /tmp/ownership-b.json
cmp docs/architecture/arc-01-domain-ownership.json /tmp/ownership-a.json
```

Expected: all commands exit 0; report is byte-stable.

---

### Task 5: 收口、自审与集成

**Files:**
- Modify: `docs/development/architecture/ARC-01-domain-boundaries.md`

**Interfaces:**
- Updates ARC-01.2 status only; parent ARC-01 remains incomplete.

- [ ] **Step 1: 自审规格覆盖**

逐项核对设计第 6～9 节：规则无重叠、39 个顶层落点、八 owner 非空、issues 为 0、digest 自洽、
CLI 错误路径、artifact provenance。明确记录控制流或未来 owner 变更等真实限制。

- [ ] **Step 2: 最终定向验证**

```text
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider \
  tests/unit/test_architecture_ownership.py \
  tests/unit/test_architecture_import_graph.py -q
python3 -m ruff check \
  src/naumi_agent/architecture/ownership.py \
  src/naumi_agent/architecture/import_graph.py \
  tests/unit/test_architecture_ownership.py \
  tests/unit/test_architecture_import_graph.py
python3 -m compileall -q src/naumi_agent/architecture
git diff --check
```

- [ ] **Step 3: 两阶段提交**

先提交最终源码和测试得到 H1，再生成 provenance artifact 并 amend 得到 H2；commit message：

```text
refactor(architecture): add domain ownership contract [ARC-01.2]
```

- [ ] **Step 4: 合并 main、复验、推送和清理**

main 与 origin/main 同步后 fast-forward；在 main 上重复 Task 5 Step 2 和 artifact `cmp`，成功后
推送 origin/main，删除本轮 feature worktree 与分支。
