# EVO-05.5e Candidate Bundle Admission

## 目标

消费 current EVO-05.5d Stage Advance authority 与 ARC-07.4b detached Ed25519 Build Attestation，把可信 builder
签署的获批候选无源码 bundle 真实安装到 ARC-07 immutable version slot，并执行 runtime `--version` boot probe。
本切片只形成 activation input，不切换 active pointer、不启动服务流量、不声称 deployed。

## 准入链

`EvolutionRevalidationCandidateBundleAdmissionService.admit()` 依次执行：

1. 重验 Stage Advance 仍未过期、control generation 未变化且授权进入 opt-in；
2. 从独立 trust provider 读取 exact content-addressed Build Trust Policy；
3. 加载 detached Attestation，验证 active builder key、有效窗口、Ed25519 signature 和 candidate manifest digest；
4. 重验 Attestation 的 exact source commit/tree 与 current Rollout Plan target 相等，失败时不安装任何 candidate slot；
5. 重验 current Rollout Plan/Fresh Decision，读取 exact prior Rollback Plan baseline commit/tree；
6. 通过 ARC-07 active resolver 校验 current slot、manifest bytes、immutability、Boot Receipt 与 runtime binary digest；
7. 要求 current slot source provenance 与 rollback baseline 完全一致；
8. 使用 signed manifest digest 作为 `ReleaseSlotStore.install()` 前置条件，在 source/staging/final copy 全程固定同一
   manifest 后安装 source-free bundle，关闭验签到安装之间的目录替换窗口；
9. 要求 installed slot 的 manifest/version/target/source 与 signed Attestation 和获批 target 全部一致；
10. 真实执行 candidate runtime `--version`，冻结 Boot Receipt；
11. 安装后再次重验 Stage Advance、active pointer、Build Trust Policy 和 Attestation；
12. SQLite 原子事务重验 Advance/Plan JSON exact projection，把 policy、trusted key、attestation、slot 与 boot receipt
    一起写入 content-addressed Admission。

安装发生在 active pointer 之外。进程在 install 或 boot 后崩溃只会留下可复用的 inactive immutable slot，不会改变用户当前版本。

Production Engine 从 `<install-root>/trust/trusted-builders.json` 动态加载 policy。它属于安装级 trust root，不能放进
workspace `.naumi` 并随项目任意修改；文件缺失、格式错误或无法验证时，Engine 仍可启动，但 Candidate Admission 失败关闭。
平台 installer 独立可信地配置该文件仍属于 ARC-07.4 后续交付门槛。

Hardened Admission 使用 v2 schema 和独立 append-only v2 表。早期只绑定 self-declared source provenance 的 v1 记录
原样保留审计，但读取路径不会把它们恢复为 activation authority；同一 Stage Advance 可以重新形成经过验签的 v2 Admission。

## 动态 fencing

读取 Admission 时会重新校验：

- Stage Advance 是否仍 current；
- active pointer 是否仍指向冻结的 rollback baseline；
- inactive candidate bundle bytes 与 manifest 是否仍一致且只读；
- exact Boot Receipt 是否仍绑定当前 candidate runtime binary。
- current Build Trust Policy 是否仍是 Admission 冻结的 exact policy；
- builder key 是否仍 active，签名是否仍验证，attestation 是否仍绑定 immutable slot manifest。

任何文件篡改、pointer 推进、Stage Advance 过期、control 变化、key 撤销或 policy 轮换都会让
`activation_input_authority=false`，但保留历史 Admission 供审计。

## 权限边界

- `activation_input_authority=true` 只允许后续 opt-in activation executor 消费；
- `deployment_authority=false`；
- `active_pointer_switched=false`；
- `process_started=false`；
- `rollback_executed=false`；
- `promotion_authority=false`。

## 验收结果

- 真实 baseline bundle install/boot/activate 后，candidate bundle install + boot 不改变 active pointer；
- 缺失、未知 key、撤销 key、无效 signature、manifest 漂移在安装 candidate 前安全拒绝；
- signed candidate source commit/tree 不匹配时安全拒绝，不产生 inactive candidate slot；
- active pointer 变化会撤销 activation input；
- candidate runtime bytes 篡改会撤销 slot/boot/activation authority；
- signed manifest digest precondition 会拒绝验签后被替换的 bundle，不留下不受信 inactive slot；
- additive key rotation 即使仍信任原 key，也会因 policy digest 改变撤销旧 Admission；key 撤销同样动态撤权；
- Release Slot 新增 inactive booted-slot exact resolver；
- production Engine 与公共 lazy exports 已接线；
- Candidate Admission 6 项、Build Attestation 5 项及相关 Release Slot/Engine 小模块验证通过，未运行全量测试。

## 下一切片

[EVO-05.5f1](EVO-05-5f1-opt-in-deployment-intent.md) 已消费 current Admission，通过 durable user interaction
形成当前本机安装的 explicit opt-in cohort，并冻结 previous pointer CAS digest；它仍不切换 active pointer。
EVO-05.5f2 必须消费 current 5f1 Intent，执行 ARC-07 atomic activation，激活后重读 active chain 与 candidate slot，
再形成 Deployment Receipt。若激活后 Receipt 落盘前崩溃，reconcile 必须根据 pointer generation 机械补写，
不能重复切换或虚报失败。
