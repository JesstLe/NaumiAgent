# EVO-05.7b2 Stable Promotion Runtime Observation Admission

## 目标

把 [EVO-05.7b1](EVO-05-7b1-stable-promotion-observation-contract.md) 的成功推广观察契约，绑定到一个
Population member 的 exact Stable Deployment，以及 Population Finalization 之后新启动的 managed New UI/TUI
heartbeat chain。该 Admission 只建立长期观察输入，不读取完整窗口、不计算长期指标，也不签发 `promoted` Outcome。

## 为什么必须经过 Stable Intent

Population Finalization 的 member Receipt 能证明签名成员完成了 binary-only finalization，Harness Runtime Binding 能证明
某个本机进程运行了什么 slot、pointer 与 binary；但两者都不能单独证明“这个 runtime 属于哪一个 installation member”。

7b2 使用既有 Stable Deployment Intent 的 Installation Proof 补齐身份桥梁：

1. Observation Contract 绑定 current Population Finalization；
2. Finalization member 绑定 signed Remote Finalization Receipt；
3. Remote Finalization Authorization 绑定 Stable Intent ID、member credential、candidate slot、boot receipt 与 active pointer；
4. Stable Intent 的 Installation Proof 绑定同一 Population Snapshot 与 member；
5. Stable Deployment 证明该 Intent 已 authority-bound 激活；
6. Harness Runtime Identity 逐字段匹配 Deployment 的 pointer、slot、version、target、Boot Receipt、binary、runtime path
   与 install root；
7. startup observation 绑定同一 binding、identity、surface、subject、instance 与 epoch。

因此，调用方只提交 Finalization Receipt ID、Stable Intent ID 和 runtime subject ID，不能自行提交或覆盖 member、pointer、
slot、binary、origin time、heartbeat phase 或 timeout。

## Fresh startup 与时间边界

Admission 只读取 verified ledger 的第一条样本，并要求：

- `chain_origin_kind=startup`、`chain_origin_sequence=1`；
- `heartbeat_sequence=1`、`phase=starting`；
- `previous_sample_sha256` 为空；
- origin 不早于 7b1 `window_not_before_at`，即 Population Finalization 的 `finalized_at`。

Stable stage 中已经运行的旧进程不能补算长期观察时间。若 origin 早于 Finalization，回执明确要求用户在 Finalization 后
重启 Naumi，形成新的 managed runtime subject。

## Artifact、并发和撤权

`EvolutionStablePromotionRuntimeObservationAdmission` 内容寻址并冻结：

- Contract、Population Finalization、member source 与 Remote Finalization Receipt；
- Stable Intent、Stable Deployment；
- Harness Binding、Runtime Identity、surface/subject/instance/epoch；
- pointer、slot、Boot Receipt、binary、version/target；
- startup origin sample、observed time 与 timeout。

Store 在 session SQLite `BEGIN IMMEDIATE` 内重读 exact Contract、Population Finalization、member Receipt 与 Deployment。
Harness ledger 位于独立 Harness SQLite，因此 Service 使用写前读取、写后 inspect 和动态复验，不能声称跨库原子事务。
同一 Contract/member/subject 的独立并发 writer 收敛到同一 artifact；新的 restart subject 可以形成新 Admission，不会被旧的
censored chain 永久阻断。Binding 与 origin sample 全局唯一，不能跨 member 重放。

View 分项暴露 Contract、Population member、active Deployment、Runtime Binding、startup origin 与 exact release authority。
任一 durable dependency、current authority、binding 或 origin 变化，`runtime_observation_input_authority` 立即撤销。

## 双通道

- Agent Tool：`evolution_stable_promotion_runtime_observation_admission`；
- Slash：`/evolution stable-promotion-admit-runtime <population-finalization-receipt-id> <stable-intent-id> <runtime-subject-id>`；
- New UI、CLI 与 Textual TUI 使用同一 Tool/Service；
- 只写治理 artifact，不启动、停止或重启进程，Moderate/Bypass 均不二次确认。

## 验收标准

- [x] 真实 ReleaseSlot → Stable Deployment → TerminalRuntimeLifecycle → Harness startup ledger；
- [x] exact Population member → Remote Finalization → Stable Intent/Proof → Deployment → runtime identity；
- [x] Finalization 后 startup sequence-1/not-before 约束；
- [x] 独立 Service 并发写入收敛；
- [x] wrong member/release、旧 runtime、缺失 origin 与 durable dependency 篡改失败关闭；
- [x] Agent Tool、Slash、权限规则、Engine composition 与 public lazy export；
- [x] 固定 Window、长期指标、Promoted Outcome、learning、promotion、execution authority 为 false；
- [x] 小模块 ruff、compile、pytest 和 diff check；未运行全量测试。

## 当前边界与下一步

本切片建立的是单 installation 的 observation input。当前 Service 读取该安装实例可访问的 Harness Store，不把本机 ledger
冒充远端 fleet 证据；跨安装 admission 的签名传输、幂等接收和 Population 完整性聚合仍需独立 authority。

后续 [EVO-05.7b2a1](EVO-05-7b2a1-stable-promotion-runtime-admission-signed-delivery.md) 已完成独立 domain 的
installation 签名、durable outbound、Control Plane current Credential/Contract/Finalization 复验与幂等 Receipt；它没有
冒充已完成网络投递。[EVO-05.7b2a2a](EVO-05-7b2a2a-fenced-runtime-admission-delivery-worker.md) 已完成 durable
fenced Worker 与真实本地 Control Plane adapter；
[EVO-05.7b2a2b](EVO-05-7b2a2b-authenticated-runtime-admission-http-transport.md) 又完成 mTLS HTTP 与配置自动装配；
[EVO-05.7b3a](EVO-05-7b3a-stable-promotion-observation-chain-cursor.md) 已按 7b1/HAR-10.2j 建立 installation 本地可恢复
chain cursor。下一步 7b3b 先签名交付 revision，再继续 Population aggregation：
分页读取每个 admitted chain，形成
`insufficient / passing / breached / censored` 的 Population 长期评估。任何单 member Admission 都不得冒充完整稳定推广。
