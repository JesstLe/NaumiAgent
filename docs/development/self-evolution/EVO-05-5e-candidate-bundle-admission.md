# EVO-05.5e Candidate Bundle Admission

## 目标

消费 current EVO-05.5d Stage Advance authority，把获批候选的无源码发行 bundle 真实安装到 ARC-07 immutable
version slot，并执行 runtime `--version` boot probe。本切片只形成 activation input，不切换 active pointer、不启动服务流量、
不声称 deployed。

## 准入链

`EvolutionRevalidationCandidateBundleAdmissionService.admit()` 依次执行：

1. 重验 Stage Advance 仍未过期、control generation 未变化且授权进入 opt-in；
2. 重验 current Rollout Plan/Fresh Decision；
3. 读取 exact prior Rollback Plan baseline commit/tree；
4. 通过 ARC-07 active resolver 校验 current slot、manifest bytes、immutability、Boot Receipt 与 runtime binary digest；
5. 要求 current slot source provenance 与 rollback baseline 完全一致；
6. 使用 `ReleaseSlotStore.install()` 校验并安装 source-free bundle；
7. 要求 installed slot source commit/tree 与获批 target 完全一致；
8. 真实执行 candidate runtime `--version`，冻结 Boot Receipt；
9. 安装后再次重验 Stage Advance 和 active pointer 未变化；
10. SQLite 原子事务重验 Advance/Plan JSON exact projection 后写入 content-addressed Admission。

安装发生在 active pointer 之外。进程在 install 或 boot 后崩溃只会留下可复用的 inactive immutable slot，不会改变用户当前版本。

## 动态 fencing

读取 Admission 时会重新校验：

- Stage Advance 是否仍 current；
- active pointer 是否仍指向冻结的 rollback baseline；
- inactive candidate bundle bytes 与 manifest 是否仍一致且只读；
- exact Boot Receipt 是否仍绑定当前 candidate runtime binary。

任何文件篡改、pointer 推进、Stage Advance 过期或 control 变化都会让
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
- candidate source commit/tree 不匹配时安全拒绝，inactive slot 不获得 Admission；
- active pointer 变化会撤销 activation input；
- candidate runtime bytes 篡改会撤销 slot/boot/activation authority；
- Release Slot 新增 inactive booted-slot exact resolver；
- production Engine 与公共 lazy exports 已接线；
- Candidate Admission、Release Slot、Engine 共 15 项小模块测试通过，未运行全量测试。

## 下一切片

EVO-05.5f Opt-in Deployment Executor 必须消费 current Admission，以冻结的 previous pointer digest 作为 CAS 前提，
调用 ARC-07 atomic activation；激活后重读 active chain 与 candidate slot，绑定 opt-in exposure cohort 并形成 Deployment Receipt。
若激活后 Receipt 落盘前崩溃，reconcile 必须根据 pointer generation 机械补写，而不能重复切换或虚报失败。
