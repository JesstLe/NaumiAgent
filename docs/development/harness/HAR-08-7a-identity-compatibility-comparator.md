# HAR-08.7a Identity Compatibility Comparator

## 1. 目标

在任何分数、通过率、成本或延迟运算前，先判断选定 Baseline 与当前 Eval 是否属于可解释的
比较空间。比较器必须允许源码 revision 变化——源码变化正是回归比较的对象——同时拒绝把不同
Suite、Profile、Runner、live 模式或模型能力的结果混为一组。

本切片交付 directional `baseline -> current` 身份兼容性 gate、typed differences 与中文解释。
它不实现指标门槛、置信区间、波动判断、Baseline 查询或持久化。

## 2. 三层差异语义

### 2.1 Blocking

以下差异令状态变为 `incompatible`，后续不得计算或展示“提升/下降”：

- Baseline 本身 `baseline_eligible=false`；
- Identity schema version 不同；
- Suite ID 或 Suite SHA-256 不同；
- Harness Profile SHA-256 不同；
- Runner version、repetitions 或 live 开关不同；
- 一侧是 `no_model`、另一侧调用模型；
- 模型 target、capability digest/status、reasoning effort/contract 不同。

比较器逐字段判断，不直接比较最终 `identity_sha256`。最终摘要包含 source revision，因此用摘要
相等作为兼容条件会错误拒绝全部真实代码变更。

### 2.2 Caveat

以下差异允许继续比较，但状态为 `comparable_with_caveats`：

- OS family 不同；
- OS release、CPU architecture、Python runtime 不同；
- NaumiAgent 发布版本不同；
- 当前结果来自脏工作区；
- 当前 Profile 未信任；
- 当前模型 capability contract 为 `partial`；
- 当前 identity 因其他治理原因不可晋升。

平台变化下功能/协议指标仍可比较，性能、成本和延迟指标必须由 HAR-08.7 后续模块分组或降权，
不能直接宣称回归。

### 2.3 Informational

Git commit 与 tree fingerprint 差异记录为 typed difference，并设置 `source_changed=true`，但不产生
blocking/caveat。完全相同的 source 表示重复运行，也合法可比较。

## 3. Directional 治理

- Baseline 必须已经满足晋升资格；不可信 Baseline 直接拒绝。
- 当前结果可以是 provisional；比较器返回 caveat，而不是丢弃即时调试价值。
- `current_provisional` 只表示当前结果不能晋升，不等价于指标失败。
- Baseline/Current 顺序不可交换：交换后资格判断可能得到不同结论。

`HarnessEvalBaselineIdentity` 同时加强反序列化不变量：即使外部重新计算合法 digest，也不能构造
`dirty=true`、`profile_trusted=false`、模型 unverified/incompatible 或 reasoning warning 与
`baseline_eligible=true` 并存的对象。

## 4. Typed 输出

`EvalIdentityComparison` 包含：

- `status`：`comparable`、`comparable_with_caveats`、`incompatible`；
- Baseline/Current identity SHA-256；
- `source_changed`、`platform_changed`、`current_provisional`；
- 有序去重的 `blocking_codes` 与 `caveat_codes`；
- `EvalIdentityDifference[]`：dimension、stable code、截断后的安全值，以及
  `blocking/caveat/informational` 三态 severity；`blocking` 便捷属性只作为派生读取。

差异值最长 512 字符；SHA-256 只显示前 12 位。不得把嵌套 identity、工作区路径或原始配置正文
复制到用户文案。

## 5. 中文解释

`render_eval_identity_comparison()` 显示：

- 可比较状态；
- Baseline 与当前短 identity；
- 是否发生源码变化；
- 稳定 code 对应的阻断原因和比较提示。

该 renderer 目前供后续 Baseline CLI/UI 复用，尚未增加一个无法选择真实 Baseline 的空命令入口。

## 6. 已验证场景

- 同身份重复运行可比较且 `source_changed=false`；
- 真实 Git 仓库两个干净 commit 可比较且 `source_changed=true`；
- 六个配置维度逐项变化均硬阻断；
- no-model/model presence、capability context 与 reasoning effort 变化硬阻断；
- Linux→macOS、架构/Python/Naumi 变化产生三类 platform caveat；
- 当前脏树或未信任 Profile 可 provisional 比较；
- 脏树或未信任 Baseline 被拒绝；
- 中文 renderer 不输出完整 tree digest；
- 重新计算 identity digest 后伪造治理资格仍被 Pydantic 拒绝。

## 7. 后续模块

- HAR-08.7b：从两个 compatible `HarnessEvalSuiteResult` 计算 case transition 与机械指标 delta；
- HAR-08.7c：绝对门槛、guardrail 与 regression verdict；
- HAR-08.7d：重复样本、均值/离散度/置信区间与 flaky 标记；
- HAR-08 H5/HAR-08.8：持久化 Baseline、选择查询、Slash/Tool/API 和全屏详情。
