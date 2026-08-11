# EVO-05.5f5x3 Stable Rollout Member Finalization

## 目标

消费 [EVO-05.5f5x2](EVO-05-5f5x2-stable-rollout-authorization.md) 的 exact、短期、single-use
Authorization，在真实 ARC-07 release store 中对 expected active pointer 做 writer-fenced CAS，并形成单 installation
member 的 binary-only Stable Rollout Finalization Receipt。

本切片完成的是一个成员的 stable finalization，不是整个 signed Population 的 rollout completion。它不更新 Release Channel
Catalog，不重复部署或切换 pointer，不执行配置/数据迁移，也不授予 promotion authority。

## 为什么 Channel Catalog 不是本动作的写目标

ARC-07.5d1 Channel Catalog 是发布者对 `channel + target -> exact signed build` 的发现权威；5f5x3 处理的是某一安装成员已经完成
stable-stage 后的本地 rollout 收口。让成员执行器修改 Catalog 会混淆 builder/channel signer 与 installation rollout authority，破坏双
信任根，因此明确禁止。

## 两数据库恢复协议

Evolution evidence DB 与 `release-slots.db` 无法共享单个 SQLite 事务。本实现使用可机械恢复的三段协议：

1. 动态 inspect x2 Authorization，确认 Completion、Rollback Readiness、kill-switch generation、TTL 和未消费状态仍成立；
2. 用固定 consumer identity 消费 durable nonce，取得 single-use Consumption Receipt；
3. 在 `release-slots.db` 的 `BEGIN IMMEDIATE` 内重读并验证完整 active pointer chain，再将 pointer ID/SHA/generation 与
   Authorization authority 原子写入 `ReleaseStableMemberFinalization`；最后把 Authorization、Consumption 和 release event 绑定为
   Evolution Completion Receipt。

若进程在第 3 步 release event 落盘后、Evolution Receipt 写入前崩溃，重试会按 Authorization ID 找回 exact release event 和
Consumption Receipt，幂等补写 Completion，不会重复动作。若 pointer 在 release CAS 前变化，finalization 拒绝且不会留下伪成功事件。

## 时间与 kill switch 证据

Completion Receipt 要求：

- Consumption 时间不晚于 release finalization；
- release finalization 严格早于 Authorization expiry；
- Evolution Store 写入时按 `finalized_at` 回看 rollout-control history，确认该时刻最新 generation 正是 Authorization 绑定的
  active generation；
- 后续 pointer 或 authority source 漂移不会删除历史完成事实，但会撤销 `active_stable_member_authority`。

这一区分避免把“过去确实完成”与“当前仍是 active stable member”混为一谈。

## 产品入口

- Agent Tool：`evolution_stable_rollout_finalization`；
- 执行：`/evolution stable-rollout-finalization execute <authorization-id>`；
- 检查：`/evolution stable-rollout-finalization inspect <authorization-id>`；
- Tool 与 Slash 共享同一 Service 和 renderer；不显示 nonce；无需二次确认；所有权限模式可用；
- renderer 明确显示 Population rollout authority 和 Promotion authority 均为 `false`。

## 验收结果

- 六路、两个 Service 的并发执行收敛为一个 release finalization 和一个 Evolution Receipt；
- 真实 immutable slot、Boot Receipt、active pointer、Population Completion 与 Authorization 全链执行通过；
- release store 在同一 writer transaction 内验证 expected pointer ID/SHA/generation；
- pointer 在执行前变化时，Authorization 不被消费且 release event 不产生；
- finalization 后发生 rollback，历史 Completion fact 保留，但 active authority 动态撤销；
- strict JSON round-trip、Tool、Slash、public exports、Engine composition、权限、Ruff、compile 与相关小模块测试通过；
- 按用户要求未运行全量测试。

## 自我审视与下一步

本切片是真实本地 release-store 动作，不是只写一张“成功”证书。但它仍不是跨控制面的 fleet publisher：当前 x2 Authorization
没有 exporter/signature envelope，远端 worker 不能把本地 SQLite artifact 当作网络 bearer token。

真实 Population 包含跨机器 member，因此不能让本机 Release Store 冒充 fleet source。
[EVO-05.5f5x3a](EVO-05-5f5x3a-authenticated-remote-readiness-claim.md) 已先补 installation Credential-bound
challenge/signature/claim；[EVO-05.5f5x3b](EVO-05-5f5x3b-remote-release-store-probe.md) 已继续让目标安装真实重验
Release Store 并返回短期 installation-signed binary readiness；
[EVO-05.5f5x3c](EVO-05-5f5x3c-signed-remote-finalization-authorization.md) 已进一步交付独立 Rollout Control key
签名的逐 member portable capability、Trust Policy 验证与 single-use consumption ledger。
[EVO-05.5f5x3d](EVO-05-5f5x3d-remote-stable-member-finalization-executor.md) 已完成手动 portable transport 下的 signed
Execution Grant、目标 Release Store CAS、installation-signed Result 与 Control Plane Receipt。
[EVO-05.5f5x3e](EVO-05-5f5x3e-remote-finalization-delivery-recovery.md) 已补 durable delivery、installation-signed ACK、
claim/retry fencing、目标 journal 与受限 late-result recovery。
[EVO-05.5f5x3f](EVO-05-5f5x3f-remote-finalization-delivery-worker.md) 已增加认证 transport Protocol、本机安装 adapter、周期 claim、
ACK timeout、retry budget、dead-letter 与 shutdown drain。
[EVO-05.5f5x3g](EVO-05-5f5x3g-authenticated-remote-installation-http-transport.md) 已继续交付独立 mTLS endpoint、同连接服务端
证书 pin、客户端证书授权、current/next 轮换、严格限长/timeout 与 Runtime 自动装配。后续仍需 Result 主动回传与安装端 daemon；只有每个
member 都形成 current readiness、Authorization 与 Finalization Receipt，才可
聚合 population-level Stable Rollout Completion Authority。配置/数据 finalization 继续等待 ARC-07.6，Promotion authority
继续独立。
