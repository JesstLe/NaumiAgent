# EVO-05.5b Runtime Observation Receipt

## 目标

将 EVO-05.4b2 local-canary terminal journal 与 EVO-05.5a frozen baseline、Rollout Plan guardrails 和 HMAC-attested
control signals 机械合并为 `insufficient|passing|breached` Observation Receipt。

## 判定顺序

1. 动态复验 Plan、Baseline、entry 与全部 content digests；
2. 读取 entry 的 ordered terminal events，并只计算真实 terminal run；
3. 从 entry 绑定的 control sequence 开始验证完整 HMAC hash chain；
4. 任一后续 pause 立即形成 breach：user actor 映射为 user withdrawal，`security_*` 与
   `data_integrity_*` reason 映射为相应 incident，其余为 rollout control pause；
5. 有真实样本时，错误率、p95 latency regression、completion-rate drop 或 cost regression 超过冻结阈值可提前 breach；
6. 没有 breach 时，样本数或观察时间不足只能为 insufficient；
7. 仅当所有门均满足时才为 passing。

Safety breach 优先于 insufficient，避免以“样本还不够”为理由忽略用户撤回或事故。零样本不会计算成 completion regression。

## 指标

- completed/passed/failed runs 与 error/completion basis points；
- terminal check duration 的 per-run sum 与 nearest-rank p95；
- 相对 frozen GREEN baseline 的 non-negative latency/completion/cost regression；
- local-canary executor 不调用模型，因此本阶段当前 cost 为可证明的 0 micro-USD；
- exact terminal event 与 control signal ID/digest tuples。

Observation 为 append-only content-addressed artifact。`breached` 只开放 pause/rollback input authority；`passing` 仍不开放
stage advance 或 promotion。

## 验收结果

- 空 journal 返回 insufficient，不把 0/0 completion 当作 breach 或 passing；
- 达到 Plan 最小 run prefix 和 observation window 后形成 passing，但 stage advance 仍为 false；
- 首次真实 check failure 可在最小样本数前提前 breach；
- user pause 在无 run 时仍形成 user-withdrawal breach；
- control history 从 entry generation 开始验证完整 hash chain；
- Engine 与公共 lazy exports 已接线；
- 只运行相关小模块测试，未运行全量测试。

## 下一切片

EVO-05.6a 自动暂停协调器必须消费 exact breached Observation，以 monitor actor 幂等触发 workspace kill switch，并签发只读
rollback request；不能在本切片中把 breach 文本直接当作已回滚。随后 EVO-05.6b 才执行 rollback plan 并验证至少一个可启动版本。
