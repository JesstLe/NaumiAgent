# UI-10.6b1 Workbench Proposal 延后交互

## 1. 目标

在既有 Workbench Reviews 页补齐 `ProposalAction.DEFER` 的 New UI 与 Textual TUI 用户入口，
让用户可以把暂时缺少证据、等待依赖或不适合立即裁决的 open Proposal 延后，而不是被迫批准或拒绝。

该切片复用 HAR-09.5b1 已存在的 Proposal 状态机、CAS、cooldown 和审计事件；不创建第二套
Proposal Store，不修改 Candidate eligibility，也不把 defer 变成执行、实验或 promotion 权限。

## 2. 依赖与唯一权威

```text
open Workbench Proposal
        |
        v
New UI / Textual TUI collects reason + bounded preset
        |
        v
PermissionChecker(workbench_govern_proposal)
        |
        v
server authority clock -> exact defer_until
        |
        v
WorkbenchService.govern_proposal(DEFER)
        |
        +--> CAS state = deferred
        +--> cooldown_until
        +--> proposal.deferred audit event
        +--> authoritative Workbench snapshot
```

`src/naumi_agent/workbench/proposal_governance.py` 继续拥有合法时间边界和状态转换规则。
`proposal_defer_until_for_preset()` 只把 UI 的 `1|7|30` 天预设转换为 authority 时钟上的 ISO 时间；
最终仍由 `plan_proposal_transition()` 机械校验 1 小时至 90 天边界。

## 3. Typed 协议

复用已协商能力 `workbench_proposal_actions` 和已有事件：

- client：`workbench/proposal/action`；
- server：`workbench/proposal/action_result`；
- action 新增允许值 `defer`；
- defer 必须携带非空 `decision_note`、严格整数 `defer_days` 和布尔 `confirmed`；
- `defer_days` 只允许 `1|7|30`，拒绝布尔、浮点、字符串和任意自定义大值；
- 非 defer action 不接受非零 `defer_days`；
- 服务端结果允许 `action=defer`，成功 Proposal 必须来自权威 Snapshot/Service。

客户端不提交自己的绝对时间，避免本机时钟漂移、时区或恶意未来时间改变治理边界。Bridge 与 TUI
在真正写入前使用 Python authority clock 生成 `defer_until`。

## 4. 权限与交互状态机

### Normal / permissive / moderate / strict

```text
d -> reason input -> 1/7/30 day choice -> confirm -> loading -> result
```

- 原因为空不能继续；最多 2000 字符且不保存未提交草稿；
- 时长通过三个键盘预设选择，不要求用户手写 ISO 日期；
- 最后一次确认仍由现有高风险 `workbench_govern_proposal` 规则要求；
- loading 阶段吞掉重复按键，CAS 冲突返回权威状态。

### Bypass

```text
d -> reason input -> 1/7/30 day choice -> loading -> result
```

填写原因和选择时长属于动作参数，不是二次确认。参数齐全后直接发送 `confirmed=false`，不再出现
额外确认页；但 Service 状态机、CAS、时间边界、cooldown 和审计一个都不跳过。

## 5. New UI

- open Proposal 详情增加 `d 延后`；
- 原因输入沿用 Unicode-aware composer 编辑，Esc 取消且不写状态；
- 时长页显示 `1 一天 · 2 七天 · 3 三十天`；
- 确认页显示精确选择天数，成功回执显示权威 `cooldown_until`；
- 返回的 Workbench Snapshot 移除 deferred Proposal 的 open review 入口；
- 80/120/200 列均保持提示、表单和列表不越界。

## 6. Textual TUI fallback

- Reviews 页增加 `d` binding，并复用同一个 `ProposalDecisionScreen`；
- 弹窗提供必填原因和仅接受 `1|7|30` 的键盘输入；默认 7 天；
- normal 提交构成明确确认，bypass 弹窗只收集必需参数并使用 `confirmed=false`；
- 直接调用相同 PermissionChecker、authority preset helper 和 Workbench Service；
- 成功后重新读取权威 Snapshot，不在 TUI 内手工修改 Proposal 状态。

## 7. 验收标准

| 场景 | 预期 |
| --- | --- |
| 原因为空 | New UI/TUI 均阻止提交并显示中文提示 |
| 选择 1/7/30 天 | 后端 authority clock 生成对应 ISO cooldown |
| `7.0`、`"7"`、2、90 或布尔 | 协议/helper 失败关闭 |
| normal 模式 | 参数收集后仍需一次明确确认 |
| bypass 模式 | 参数齐全后立即发送，无二次确认 |
| 并发已有终态 | Service CAS 冲突，不覆盖权威状态 |
| 真实 SQLite | Proposal 变为 deferred，保存 reason/cooldown，产生 `proposal.deferred` |
| 取消或未完成表单 | 不写 Store、不产生审计事件 |
| New UI/TUI | 使用同一 Service 和时间 helper，不复制状态机 |

## 8. 非目标与后续

- UI-10.6b2 Proposal merge 目标选择器、同 Candidate 较新 revision 过滤和确认已独立交付，详见
  `UI-10-6b2-proposal-merge.md`；
- waiting Approval 的 approve/reject/defer 动作；
- 自定义绝对时间、小时级任意输入或跨 Proposal 批量 defer；
- HAR-09.6 before/after outcome tracking；
- 任何代码执行、Experiment Contract 签发、merge、promotion 或 Git 写入。

因此 UI-10 与 HAR-09 仍为 `partial`。UI-10.6b2 后，Proposal defer/merge 入口已经齐备，但 waiting
Approval 动作与 HAR-09.6 Outcome authority 仍未完成。
