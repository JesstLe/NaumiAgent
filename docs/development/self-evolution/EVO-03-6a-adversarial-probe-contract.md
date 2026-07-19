# EVO-03.6a Adversarial Probe Contract

## 目标

把“做过边界、并发、安全、恢复、跨平台、奖励投机测试”从自然语言声明改为机械 authority。该切片只负责
选择必须执行的 probes、绑定当前可信 Harness Profile 的真实 checks、报告缺口并形成防篡改 Contract；不执行
命令、不生成模型判断、不授予 Shell 权限，也不复制 HAR-08.4e/4f worker 或 Batch 状态机。

## 类型化 Profile 能力

`HarnessCheckSpec.adversarial_probes` 可显式声明六类能力：

- `boundary`
- `concurrency`
- `security`
- `recovery`
- `cross_platform`
- `reward_hacking`

字段默认空，排序且禁止重复。`provides: [unit]` 不会自动推导为 `boundary`，文件名包含 `security` 也不会让
普通 check 自动获得安全探针能力。Profile 原始字节仍参与 Profile digest；新增或修改能力标签后必须重新信任。

## 机械 Registry

`EvolutionAdversarialProbeRegistry` 以版本化定义将 changed path 映射到要求：

1. 每个变更文件必须执行 `boundary-v1`；
2. runtime/orchestrator/queue/worker 等路径增加 `concurrency-v1`；
3. safety/permission/auth/config 等路径增加 `security-v1`；
4. store/memory/checkpoint/recovery 等路径增加 `recovery-v1`；
5. UI/TUI/CLI/terminal/shell/platform 等路径增加 `cross-platform-v1`；
6. evolution/harness/eval/metric/policy 等路径增加 `reward-hacking-v1`。

Registry 定义自身形成 `registry_sha256`。跨平台探针声明 `platform_scope=matrix`，其余当前为 `current`。
这些模式是可审查的保守触发规则，不是 LLM 分类结果；后续扩大规则必须提升 probe version 或 Registry identity。

## Contract authority

Builder 在产生 Contract 前重新验证：

- Validation Plan 和 Profile Binding 的 ID/SHA/Profile identity 一致；
- 工作区 `.naumi/harness.yaml` 当前有效且 digest 未漂移；
- Harness Trust Store 当前仍信任该精确 digest；
- 每个 `(path, probe kind)` 仅绑定一个同时满足 `required_for/when_changed` 和 `adversarial_probes` 的 check。

Contract 绑定 Validation Plan、Profile Binding、candidate revision/files digest、Registry、当前平台身份和所有
check spec/argv digest。它不保存 argv 明文。缺少 check 产生 `probe_check_missing`，多个 check 产生
`probe_check_ambiguous`；每条 requirement 必须恰好进入 coverage 或 blocker，二者不可重叠。任何嵌套字段漂移
都会使 Contract 校验失败。

## 为什么仍是不可执行

即使 `coverage_complete=true`，本切片仍固定 `execution_ready=false` 和
`runner_binding_status=required`。这是为了防止 Contract 被误当作测试结果：

- 它只证明可信 Profile 声明了唯一 probe check；
- 它尚未证明 check 在 RED/GREEN candidate 上运行；
- `cross_platform` 的 `matrix` 还没有多平台 receipts；
- probe metric、guardrail 和 repeated cohort 尚未生成 H5a/H5c evidence。

下一切片必须把完整 Contract 转换为不可执行 Adversarial Batch Request，然后复用 HAR-08.4e/4f 获取权限、
执行与恢复；不得让 `coverage_complete=false` 的 Contract 进入 runner。

## 验收标准与证据

- 真实临时 Git 工作区完成 Mutation Receipt → Validation Plan → trusted Profile Binding → Probe Contract；
- UI Python 变更机械要求 `boundary/current` 与 `cross_platform/matrix`，并分别绑定唯一真实 Profile check；
- 同 authority 和平台输入得到同一 Contract identity，Contract 不泄漏 argv；
- 无标签 Profile 返回两个 missing blockers，不把普通 unit/contract check 冒充 adversarial evidence；
- 两个 boundary checks 返回 ambiguous blocker，候选 IDs 稳定排序；
- Profile 一字节漂移在 Contract 构建前阻断；coverage/check 嵌套篡改无法反序列化；
- Engine 暴露共享 Builder；Ruff、编译和定向测试通过，未运行全量测试。

## 当前不足与下一步

当前没有执行 probe、没有跨平台 CI matrix receipt、没有 adversarial H5a/H5c，也没有最终 Evaluation Receipt。
`path_patterns` 是显式保守规则，但仍需随着真实风险目录新增版本化定义。下一步实现 EVO-03.6b
Adversarial Batch Request：冻结 ordered requirements/checks、RED/GREEN target、repetitions、预算和平台矩阵，随后
由 HAR-08.4e/4f 执行；本模块不得新增 runner。
