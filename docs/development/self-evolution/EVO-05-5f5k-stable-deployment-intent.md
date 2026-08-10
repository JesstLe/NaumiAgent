# EVO-05.5f5k Stable Deployment Intent

## 状态

部分实现。最小 ARC 前置 `ARC-07.5d3 stable_deployment_intent_input_authority` 与 5f5k1 installation
proof-of-possession 已交付；Intent Service/Store/View 尚未实现。

## 目标

把 current `percentage → stable` Stage Entry Authorization、current signed managed-installation Population、exact verified
Archive Admission、本机 target 与 previous active pointer CAS 组合为逐安装、短期、一次性的 Stable Deployment Intent。

本模块授权“Population 中这一台持钥安装可准备部署 stable 的 exact inactive slot”，不执行 boot、pointer switch、进程启动或
100% exposure，也不把单台 Intent 数量冒充 stable rollout 完成度。

## 为什么不能复用 Percentage Assignment

Percentage Assignment 的职责是从完整 Population 中选出小于 100% 的有限 cohort。stable 的计划目标固定为 100%；若 stable
继续要求 `member_selected=true`，只会重复授权已进入 percentage 的少数安装，无法覆盖未入选成员。

5f5k 因而必须消费 current complete Population Snapshot，并允许其中任一 current Credential 请求 Intent。调用者仍必须用该
Credential 绑定的 installation private key 对 exact challenge 签名；客户端不能只提交 `member_id` 冒充安装身份。private key
不得进入 Intent、SQLite、日志或错误信息。

## Exact authority 输入

每次签发必须同时动态重验：

1. current 5f5j Receipt 为 `advance`，`stable_stage_entry_authority=true`，且 Plan/control/interaction/TTL 仍 current；
2. current Rollout Plan 与 5f5j exact ID/digest 一致，stable stage exposure 为 100%；
3. current、latest、Registry-signed complete Population Snapshot 的 channel 与 admitted archive channel 一致；
4. exact Credential 属于 Snapshot，签名、有效期、member/public-key projection 均有效；
5. installation private key 对包含 Stage Advance、Snapshot、Credential、Plan、Admission、host target、previous pointer 和签发时间的
   domain-separated challenge 完成 Ed25519 proof-of-possession；
6. Archive Admission View 的 `stable_deployment_intent_input_authority=true`，slot 完整、immutable 且 inactive；
7. Catalog/build source commit/tree 与 Plan target 完全一致，本机 target platform 属于 Plan required platforms；
8. existing active pointer 与 frozen previous pointer 完全相同，candidate slot 不等于 current slot，下一 generation 机械等于
   `previous.generation + 1`。

## Artifact、Store 与动态撤权

- Intent 使用 strict/frozen model 与 canonical SHA-256 content identity；
- authority window 最长 300 秒，并截断到 5f5j Receipt、Population Snapshot 与 Credential 的最早过期时间；
- 以 `stage_advance_receipt_id + credential_id + admission_id` 唯一，一台安装对 exact stable authority source 只能签发一次；
- Store 在 `BEGIN IMMEDIATE` 内重读同库 Evolution source，并在写前后重验跨库 Population、Admission、slot 与 pointer；
- View 每次 inspect 重验所有 source；Snapshot 换代、Registry key 撤销、control pause、用户交互失效、archive/slot 篡改、pointer 推进、
  host target 改变或 TTL 到期都会撤销 authority；
- artifact 大小有界，SQLite JSON 篡改 fail closed，不持久化任何 private key、下载 token 或原始机器标识。

## 5f5k1 已实现：Stable Installation Proof

`revalidation_stable_installation_proofs.py` 已实现独立的 strict/frozen Ed25519 proof primitive：

- domain-separated canonical challenge 冻结 Stage Advance、Population、Credential、Plan、Admission、slot、channel、host target、
  previous pointer generation/digest、100% exposure 与签发时间；
- proof 嵌入 Credential，仅使用其中 installation public key 验签；独立 primitive 不验证 Registry Trust Policy 或 current
  Snapshot membership，并将 `population_membership_verified` 固定为 false；
- Credential ID/digest/member/channel 与 challenge 必须完全一致，错误 key、替换 Credential 或修改任一 challenge 字段均失败；
- `percentage_selection_required=false`，避免把 limited cohort 误作 stable population；
- private key 只存在于注入的异步 signer port，不进入 artifact；Proof 固定所有 deployment/rollout authority 为 false。

该 primitive 尚不自行证明 Snapshot、Stage Advance、Admission 或 pointer “current”；5f5k Intent Service 必须先后动态读取这些
authoritative source，在签名前后重验，并将 Proof 嵌入 durable Intent。UI 不得把独立 Proof 显示为可部署。

## 权限边界

current View 只开放 `stable_deployment_intent_authority=true` 与下一层 `boot_preparation_authority=true`。以下字段固定 false：

- `boot_executed`、`active_pointer_switched`、`process_started`；
- `stable_installation_exposure_authority`、`stable_rollout_authority`；
- `promotion_authority`、`rollback_authority`、`publish_executed`。

后续必须分别形成 Stable Prepared Receipt、Activation/Recovery Receipt、Runtime Exposure/Window/Outcome，并以 authoritative
Population denominator 聚合；只有所有策略门槛满足后，才允许声明 stable rollout 状态。

## 小模块验收标准

- 真实 Ed25519 Registry/installation key、signed Population、signed archive、immutable slot 与 active baseline 完成端到端签发；
- percentage 未选中的 current Population member 也能凭 PoP 签发，非成员、错 key、篡改 challenge 均失败；
- 多 Service 并发对 exact source 幂等收敛，不同 Credential 各自独立；
- Snapshot 换代、Credential/Registry 撤权、5f5j TTL/control 变化、archive/slot 篡改、host target 或 pointer 变化均动态撤权；
- Intent 过期不可直接重放，durable JSON 篡改 fail closed；
- 全链路没有 boot、activation、runtime、Git 或 publish 副作用；
- 仅运行相关 pytest 小模块、ruff、compile 与 public lazy import，不运行全量测试。

5f5k1 当前验收：真实 Registry 签发的 Credential + installation Ed25519 key 验签成功；错误私钥与替换 Credential 失败；artifact 不含
private-key material；2 项小模块测试、ruff、compile 与 public lazy import 通过。

## 后续切片

5f5k 实现后，下一切片是 Stable Boot Preparation。它应复用 percentage boot executor 的隔离 probe/claim/lease 核心，但使用独立
typed Receipt 和 stable authority source，不能通过改字段把 Percentage Prepared Receipt 冒充 Stable Prepared Receipt。
