# EVO-05.5f5v Default Stable Read Graph

## 目标

把 [EVO-05.5f5u](EVO-05-5f5u-dynamic-stage-completion-inspection-port.md) 的可注入 5f5r inspection port 变成默认生产能力。
普通 `naumi` 启动不再永久显示动态端口 `missing`；当存在 Stable Population candidate 时，Engine 会从现有 durable roots
恢复完整 opt-in → percentage → stable evidence graph，并调用原 5f5r Service 的 `inspect()`。

该切片只恢复和读取 authority graph，不增加任何发布写入口，也不授予 stable rollout 或 promotion authority。

## 生产依赖根

Engine 复用已经真实组合的对象，而不是复制状态或创建第二套 authority source：

- Evolution evidence DB：Rollout Plan、Baseline、Control 和所有阶段 receipt；
- opt-in Runtime Health Service：作为后续 Window/Outcome/Completion 的最下游 current runtime root；
- HAR Store：interaction、Binding、heartbeat 与 observation ledger；
- ChatRun Store：真实 run、Completion Receipt 与 usage；
- Release Population Store：current Snapshot、Registry trust、有效期和成员凭据；
- Artifact Fetch Service 与 Archive Admission Store/Service：download/build trust/installed slot；
- Release Slot Store：candidate slot、boot receipt、activation history 与 active pointer。

工厂首先核对 workspace identity，以及 opt-in health、Plan、Baseline、Control 是否共享同一 canonical Evolution DB。任何错配在
Engine 组合时立即失败，不允许读取一半来自测试库、一半来自生产库的混合 authority。

## Source-lazy 组合

`EvolutionLazyStableReadGraphInspector` 在 Engine 启动时只保存一个同步 factory：

1. 不打开 SQLite；
2. 不创建 schema、目录或 release state；
3. 不读取 Population/build/channel trust artifact；
4. 不访问钥匙串或 installation private key；
5. 空候选 Tool/Slash 调用不会初始化 read graph。

第一次有 durable candidate 需要动态检查时，async lock 保证 factory 只初始化一次。随后所有成员复用同一只读图，由上层 5f5u
继续执行每批最多 16 个成员的动态检查。显式 `RuntimeServiceOverrides.stable_stage_completion_inspector` 始终优先于默认 factory，
便于嵌入端和测试提供受控实现。

## 只读封装

内部重建的 Service 链仍是生产类型，以便逐层复用既有 `inspect()` 逻辑：

```text
opt-in health
  -> opt-in window -> outcome -> completion -> advance
  -> percentage assignment -> intent -> boot -> deployment -> exposure
  -> percentage window -> outcome -> completion -> advance
  -> stable intent -> boot -> deployment -> exposure
  -> stable window -> outcome -> 5f5r completion
```

对外只返回 `EvolutionStableReadGraphInspector`，它仅公开 `inspect(evidence_id, subject_id)`，没有 `assess()`。内部需要的 installation
signer 和用户交互 callback 被固定为拒绝端口；即使调用者通过普通 Tool 获得 inspector，也不能借此签发 Assignment/Intent、推进
stage、执行 boot 或切换 pointer。

## 用户与 Agent 通道

- Agent Tool：`evolution_stable_population_candidate_preview`；
- Slash：`/evolution stable-population-preview [population-snapshot-id] [limit]`；
- New UI、Textual TUI 与 CLI 继续复用同一 Tool/renderer；
- 空候选显示端口 `configured`，但保持未初始化；
- candidate 存在时显示逐成员动态结果和总体 dynamic authority；
- 全链路只读且无需确认，bypass 不产生额外确认。

## 验收标准

- 默认 AgentEngine 组合 `EvolutionLazyStableReadGraphInspector`，显式 override 保持对象身份并优先；
- 默认 inspector 不公开 `assess()`；
- Engine 构造与空候选调用均不初始化 read graph、不读取 Population trust；
- 全部下游 Service/Store 使用 exact workspace、Evolution DB、HAR、ChatRun 与 release roots；
- 使用真实 stable fixture 重新构造一套独立 read graph，原 5f5r `inspect()` 能读取同一 durable Completion；
- 真实 insufficient Completion 被计为 dynamically revalidated 但 non-authoritative；
- read graph 检查前后 Evolution DB bytes 不变；
- Tool/Slash 仍固定 `stable_rollout_authority=false`、`promotion_authority=false`；
- 仅运行相关小测试、单一真实场景、ruff、compile、public import 与 YAML 校验，不运行全量测试。

## 自我审视与下一步

本切片解决了默认生产动态重验缺口，但 dynamic authority 仍只是一次只读预演，不是 durable Population Completion Receipt，也没有
跨进程 fencing、签名、幂等 source-set identity 或 expected-pointer 执行互锁。下一切片应实现 EVO-05.5f5w Stable Population
Completion Authority：冻结 exact current Population + 全成员 5f5r Views，动态撤权后禁止发布，并继续把 rollout/promotion 分开。

默认 read graph 目前在第一次候选检查时一次性恢复完整对象链；对象构造无 I/O，但极大 Population 的逐成员读取成本仍由 5f5u 的
16 并发上限约束。后续可以增加按 Snapshot 的短生命周期只读缓存，但缓存不得跨 trust/ledger/pointer 变更冒充 current authority。
