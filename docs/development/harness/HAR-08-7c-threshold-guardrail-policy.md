# HAR-08.7c Threshold 与 Guardrail Policy Comparator

## 1. 目标

在 HAR-08.7a Identity gate 与 8.7b Mechanical Comparator 之后，用 Suite 自身声明、进入
Baseline Identity 的 Policy 生成最终机械门槛结论。Policy 可以表达产品容忍度，但不能把 Eval
基础设施错误、跳过或未验证 guardrail 配置成“允许通过”。

本切片仍不处理重复样本、延迟统计、置信区间或 flaky 判定。

## 2. 严格 Policy Schema

`HarnessEvalComparisonPolicy` 是 frozen、extra-forbid Pydantic contract：

- `min_pass_rate`：当前绝对通过率下限，默认 1.0；
- `max_implementation_failures`：当前实现失败数上限，默认 0；
- `max_regressions`：相对 Baseline 的新增回归上限，默认 0；
- `max_pass_rate_drop`：相对 Baseline 的通过率下降上限，默认 0.0。

比例只接受有限的 0..1，计数只接受 0..500；NaN、Infinity、负数、未知字段均拒绝。Policy
用排序、无空白 canonical JSON 计算 SHA-256。

不提供 `max_evaluation_errors` 或 `max_skipped`。任何 Eval error/skip 都由 8.7b 返回
`inconclusive`，不可通过放宽门槛绕过。

## 3. Identity 与 Result 绑定

- Suite YAML 的 `comparison_policy` 进入加载后的 typed Suite；
- `HarnessEvalSuiteResult` 保存完整 Policy snapshot 与自校验 `policy_sha256`；
- `HarnessEvalConfigurationIdentity` 保存同一 `policy_sha256`，并纳入 configuration digest；
- 8.7a 对 Policy digest 差异返回 `policy_digest_mismatch` blocking；
- 8.7b 校验 Result policy digest 与自身 Identity 一致。

因此更换门槛必须生成新 Identity，不能拿同一 Baseline 暗中使用更宽松规则重新判定。

## 4. Guardrail Evidence

每个 `HarnessEvalCaseResult` 保存：

- `primary_metric`；
- 有序、不可重复的 `HarnessEvalGuardrailResult[]`；
- guardrail status：`passed`、`failed`、`unverified`。

`protocol_hello` Suite schema 必须同时声明 `no_model` 与 `no_side_effect`：

- `no_model` 由静态 Runner 代码路径直接证明；
- `no_side_effect` 初始为 unverified，只有整组 Suite 运行前后 Git fingerprint 一致时才更新为
  passed；
- 缺失或 unverified evidence 使 Policy 结论 inconclusive；
- failed evidence 产生 Policy violation；
- 即使 evidence 被错误标成 passed，只要 Current Identity 含 model，`no_model` 仍判失败。

Baseline 自身缺失、未验证或失败的 guardrail evidence 使其不可用于 Policy 比较。

## 5. Policy Verdict

- `incompatible`：Identity/Result/Policy 绑定不成立，或 Baseline evidence 不合法；
- `inconclusive`：8.7b 无法判断，或 Current primary/guardrail evidence 缺失、未验证；
- `failed`：至少一个绝对、相对或 guardrail violation；
- `passed`：全部门槛满足。

Policy verdict 与 mechanical verdict 分开。例如 Policy 明确允许 1 个新增回归时，机械状态仍为
`regressed`，Policy 可为 `passed`；renderer 必须显示“机械变化：回归（门槛允许）”。

## 6. 数值规则

- absolute：current pass rate 不低于下限，implementation failures 不高于上限；
- relative：regression count 与 `max(0, baseline_pass_rate-current_pass_rate)` 不高于上限；
- 浮点边界使用 1e-12 相对/绝对容差，避免 1/3 等可表示误差造成假失败；
- 不计算 duration、均值或显著性；这些属于 HAR-08.7d。

## 7. 已验证场景

- 默认严格 Policy 对全绿稳定 Suite 通过；
- 一个回归同时产生通过率、实现失败、回归数、通过率下降四类 violation；
- 显式容忍度可以接受有限机械回归，UI 仍显示机械变化；
- 1/3 浮点边界不产生精度假失败；
- guardrail failed 为 Policy failure，unverified/missing 为 inconclusive；
- primary metric 缺失为 inconclusive；
- Policy digest 变化被 Identity gate 阻断；
- Eval error 即使用户尝试声明未知容忍字段也无法通过 schema；
- 真实 Git 静态 Eval 生成 Policy snapshot、同 digest Identity、primary metric 与两项 passed evidence；
- Result policy digest 篡改被 Pydantic 拒绝。

## 8. 后续模块

- HAR-08.7d：重复样本、均值/离散度、置信区间与 flaky 标记；
- HAR-08 H5：持久化 Result、Policy、Identity、Comparison 与不可覆盖 Baseline；
- HAR-08.8：Baseline promote/select、Slash/Tool/API 与 New UI Policy 详情。
