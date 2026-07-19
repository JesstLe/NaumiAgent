# EVO-03.3c2c2 Interventional GREEN Continuous Cohort

## 目标

把 EVO-03.3c2b2 的单 candidate sample 扩展为完整、可中断恢复、可幂等审计的 GREEN cohort，使 HAR-08
第一次同时拥有同 Suite/Profile/seed/order/configuration/platform 的真实 RED 与 candidate GREEN 项目代码
执行证据，为 interventional H5c comparator 提供可信输入。

## 执行协议

1. 每轮只做一次无副作用 preflight：重验 GREEN Request 与 RED/Plan/Profile/Metric/Lease authority、完整
   RED H5a cohort、当前 Profile trust、active/unexpired Lease、受管 Candidate Snapshot、平台和共同
   configuration。该步骤发生在读取父权限与获取 Runtime lease 之前。
2. 读取 GREEN H5a records，要求 sample index 是连续前缀。已有 records 使用同一次 preflight 的当前 checks
   和 candidate identity 逐项验证并重建 sample receipts，不按 sample 重复扫描完整 RED cohort/Git authority。
3. 未完成时由共享 cohort kernel 获取一个 Runtime lease 和一个 cohort-scoped Run Grant，从第一个缺失
   sample 开始执行。每个 sample 仍由共用 sample kernel 在 Worker 前后复验 Profile 与 Candidate Snapshot。
4. 中断保留已完成的不可变 H5a 前缀；恢复使用新父回执、新 lease epoch 和新 Run Grant。旧 grant 永久撤销，
   旧 owner 不能继续写入。
5. completion receipt 绑定全部 sample/result digest、candidate identity/tree、configuration/platform、seed、
   Profile check 状态、typed metric values 与本 cohort 历史使用的 Run Grant digests。
6. 返回 completion receipt 前再次执行完整 preflight；Candidate、Lease、Profile、RED evidence 或平台发生变化
   时不得返回旧结论。

## Failure 与幂等语义

- implementation failure 是可信 candidate 结果，可以进入完整 cohort；evaluation error、缺 lifecycle、身份漂移
  或证据缺口不能伪装成 candidate defect。
- 完整 cohort 的重复调用不读取父权限、不签发新 grant，但仍重验当前 authority 与 Candidate Snapshot。
- GREEN Request 自摘要篡改在读取 H5a 和父权限前失败；语义 binding 不匹配在 preflight 中、Runtime lease 前
  失败。
- 只有每个 record 的 check cases 都绑定 `run_scope=cohort`、有效 grant digest 和同一 candidate identity，
  completion receipt 才能形成。

## 真实验收证据

- RED cohort 先完成 5 个真实 ARC-04 samples；GREEN 第一次执行 sample 0 后注入中断，H5a 只保留 `[0]`。
- 301 秒后使用新父回执恢复并完成 `[0,1,2,3,4]`；两轮 GREEN cohort grants 均撤销，Runtime leases 均释放。
- Candidate Profile check 五次均为可信 implementation failure；相同静态 metric 的 RED values 为
  `(0,0,0,0,0)`，GREEN values 为 `(1,1,1,1,1)`。
- 完成态无父权限幂等返回同一 receipt；candidate 文件增加一个注释后，已有 cohort 也拒绝返回。
- baseline workspace 始终保持 `sample.py == "baseline\\n"`。

## 下一步

EVO-03.4b 应复用 HAR-08 H5b2/H5c 现有 comparison authority，把本 interventional RED/GREEN completion
receipts 与 ordered H5a records 绑定成对，先做 identity/configuration/platform/check/metric 完整 gate，再调用
既有 mechanical、Policy 与 statistical comparator。不得新造 Evolution 专用评分算法，也不得直接晋升。
