# 模块实施记录模板

每个实现模型领取模块后复制本模板到交付说明或 PR 正文，并在实施过程中持续更新。空字段不
允许删除，必须填写 `not_applicable` 或解释阻塞原因。

## 1. 身份与基线

```yaml
module_id: <HAR-05>
owner: <model/person>
status: planned|in_progress|changes_required|approved|blocked
base_branch: main
base_commit: <sha>
delivery_branch: <branch>
target_commit: <single module commit>
started_at: <ISO-8601>
finished_at: <ISO-8601 or null>
dependencies:
  - id: <ARC-03>
    verified_status: implemented
```

记录启动时的 `git status --short --branch`；若工作树已有改动，列出归属和隔离策略。不得把
“用户已批准”解释为可以覆盖未提交文件。

## 2. 子模块任务账本

| 子模块 ID | 状态 | 目标文件/接口 | RED 证据 | GREEN 证据 | 真实场景 | commit |
| --- | --- | --- | --- | --- | --- | --- |
| `<ID>.1` | pending |  |  |  |  |  |
| `<ID>.2` | pending |  |  |  |  |  |

状态只允许 `pending/in_progress/verified/blocked/superseded`。每个 verified 子模块必须能追到测试
或人工可复现证据；同一模块可多次本地 commit，但最终必须交付一个清晰的模块提交序列。

## 3. 接口与数据变更

- 新增/修改的 public API、Tool schema、slash 命令和 UI event：
- Store table/index/migration/retention 变化：
- Python/TypeScript/TUI/daemon 契约变化：
- 兼容窗口、feature flag、fallback 和移除条件：
- secret、PII、reasoning、raw output 的收集与脱敏策略：

任何公共字段都要写 owner、producer、consumer、版本、默认值、未知值行为和重复/乱序行为。

## 4. RED 计划

| 风险 | 最小失败测试 | 预期失败原因 | 是否已观察 |
| --- | --- | --- | --- |
| 核心 happy path 缺失 |  |  | no |
| 空输入/错误路径 |  |  | no |
| 重复/乱序/幂等 |  |  | no |
| 取消/超时/崩溃/恢复 |  |  | no |
| 并发/背压/资源清理 |  |  | no |
| 权限/路径/隐私 |  |  | no |
| 窄屏/无色/跨平台 |  |  | no |

不是每个风险都适用于每个模块，但 `not_applicable` 必须说明为什么目标边界不可能触发该风险。

## 5. 实施序列

1. 冻结类型、状态机和错误模型，不先写 UI 装饰。
2. 完成纯规则与 Store/transport adapter 的单元 RED/GREEN。
3. 接通唯一权威 service，不创建平行状态层。
4. 接通 slash/Agent Tool/New UI/TUI 中文用户表面。
5. 运行真实内部边界集成和至少一个用户场景。
6. 做安全、恢复、并发、UX 和跨平台自审。
7. 更新证据、模块状态、兼容说明和下一模块依赖。

若模块文档规定了更严格顺序，以模块文档为准；不得通过跳过失败状态直接进入 UI 完成态。

## 6. 验证证据

```text
RED:
  command:
  exit_code:
  observed_failure:
GREEN:
  command:
  passed:
  duration:
LINT_TYPE_COMPILE:
  command:
  result:
REAL_SCENARIO:
  setup:
  trigger:
  observations:
  cleanup:
NOT_RUN:
  item:
  reason:
```

输出过长时保存 artifact 并记录 digest；证据不得包含 API key、认证 header、完整 reasoning 或
未经脱敏的用户数据。全量测试只在阶段门运行，本模板优先记录风险最高的最小定向命令。

## 7. 自我审视

- 实现是否真正满足设计，还是只让测试通过？
- 是否复制了已有 Store、Permission、Task、Harness、Worktree 或 protocol 机制？
- 作为用户，等待、失败、取消、恢复和完成是否都看得懂？
- 哪个边界情况最可能在线上出现，而当前证据最弱？
- 哪些不足无法在本模块内修复，分别指向哪个后续模块 ID？

## 8. 交付结论

列出 changed files、最终 commit、远端状态、已知不足和建议审核命令。实现模型只能建议结论，
最终的 `approved/changes_required/blocked/rejected` 由独立审核者填写。
