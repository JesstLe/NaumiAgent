# HAR-08.6a Baseline Identity 契约设计与验收

## 1. 目标

为每次 Harness Eval 生成稳定、可验证、无敏感信息的比较身份。相同结果只有在源码、Eval
配置、模型能力、思考强度和运行平台均一致时才能视为同一 Baseline 候选；禁止只用模型显示名
或 Git commit 粗略归组。

本切片只提供身份契约与晋升资格判断，不新增数据库表，不保存 Eval 结果，也不实现 Comparator。
这遵守 HAR-08 的 H5 存储边界，并为后续存储唯一键提供确定输入。

## 2. 身份组成

### 2.1 Source Identity

- `commit`：真实 Git HEAD，支持 SHA-1 与 SHA-256 object format；
- `tree_sha256`：复用 Harness `compute_tree_fingerprint()`，覆盖 HEAD、index、status、
  tracked/untracked 脏文件内容与模式；
- `dirty`：是否存在未提交状态。

身份中不保存工作区绝对路径或脏文件路径。脏树仍可产出 Eval 结果，但不可晋升为 Baseline，
避免无法从 commit 重建的结果被当作长期基准。

### 2.2 Configuration Identity

- Suite ID 与 Suite 文件 SHA-256；
- 当前受信 Harness Profile SHA-256；
- 带正整数版本的 runner identity，例如 `protocol_hello@1`；
- 重复次数与 `live` 开关。

字段经严格校验后用排序、无空白 canonical JSON 计算 `digest`。加载持久化身份时会重新计算并用
constant-time compare 检查，防止字段和 digest 被分别篡改。

### 2.3 Model Identity

- Static 与纯 Replay 等 `no_model` Runner 使用明确的 `model: null`，不受用户当前配置模型影响；
- 实际调用模型的 Sandbox/Live/Agent Runner 必须同时提供 capability 与 reasoning，禁止只提供一半；
- requested/canonical/upstream model、provider 与 API format；
- `ModelCapabilityContract` 的事实摘要，包括上下文、输出上限、请求上限、价格、Tool/streaming/
  parallel/structured/reasoning/vision、modalities、字段 provenance 与 contract status；
- `ReasoningEffortStatus` 的 effective value、来源、支持集合、默认值与是否存在兼容告警。

能力告警和错误的自然语言正文不会进入身份或持久化 payload，避免内部细节和未来文案变化污染
比较；能力事实与 status 会进入摘要。能力合同和思考强度必须属于同一个 requested model。

### 2.4 Platform Identity

- `macos`、`linux`、`windows` 或 `unknown`；
- OS release、machine architecture；
- Python implementation/version；
- NaumiAgent version。

不读取或序列化环境变量、主机名、用户名、凭据和 API Base。

## 3. Baseline 晋升规则

以下情况仍允许记录普通 Eval 结果，但 `baseline_eligible=false`：

1. Git 工作区存在未提交变更；
2. Harness Profile 尚未由用户信任；
3. 模型能力合同为 `unverified`；
4. 模型能力合同为 `incompatible`；
5. 当前思考强度与模型能力声明存在告警。

`partial` 合同允许晋升，但生成明确警告，且后续比较必须匹配完全相同的 capability digest。
最终 `identity_sha256` 覆盖全部嵌套身份、资格和结构化警告，并在反序列化时重新校验。

## 4. API

- `HarnessEvalConfigurationIdentity.create(...)`：生成带自校验 digest 的安全配置身份；
- `capture_eval_platform_identity()`：采集有界平台事实；
- `build_eval_baseline_identity(...)`：组合真实 Git fingerprint、配置、模型能力、思考强度和平台，
  返回 `HarnessEvalBaselineIdentity`。

调用方必须从 `ModelPort.get_model_capability_contract()` 与
`ModelPort.get_reasoning_effort_status()` 获取模型事实，不得自行猜测上下文窗口或思考强度。

## 5. 已验证场景

- 同一干净 Git 工作区、配置、模型和平台重复构建身份完全一致且可晋升；
- 修改 tracked 文件后 commit 不变但 tree digest 与最终 identity 改变，并阻止晋升；
- Suite digest、上下文窗口、思考强度、CPU 架构任一变化都会生成不同 identity；
- `unverified` 能力与不兼容思考强度不能晋升；
- 配置 digest 和最终 identity digest 篡改均被 Pydantic 拒绝；
- 模型能力与思考状态错配被拒绝；
- `no_model` Runner 生成稳定的 null model identity，单边提供 capability/reasoning 被拒绝；
- 未受信任 Profile 可运行离线 Eval，但身份明确阻止晋升；
- 真实 `ModelRouter` 生成的 verified capability/reasoning 可以端到端构建身份；
- 当前主机平台采集只返回有界运行时事实。

## 6. 后续依赖

- HAR-08.2-08.5 runner 将各自声明带版本的 runner identity，并生成对应 Suite digest；
- HAR-08.7 Comparator 必须先比较 identity dimensions，再进行指标差异计算；
- HAR-08 H5 存储以 `identity_sha256` 作为不可覆盖身份的一部分，并复用 ARC-05.2a 迁移内核；
- HAR-08.8 UI/Tool 显示资格、警告和短 digest，不显示能力合同或配置正文。

## 7. 当前不足

- Static `protocol_hello@1` 已由 HAR-08.6b 从 `/harness eval` 自动生成并显示身份；其他 runner
  仍需分别声明版本和相关维度；
- 尚未持久化或提供 Baseline promote 命令；
- 尚未定义跨平台聚合策略，当前只保证平台差异不会被错误合并。
