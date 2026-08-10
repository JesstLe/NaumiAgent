# HAR-09.6c2a3a Durable Worker Health 前置

## 状态

已实现。

## 为什么先做本切片

HAR-09.6c2a2a 已选中 exact Worker incarnation，6c2a2b 已解析 exact target baseline，但两者都不能证明 Worker
此刻健康、仍接受任务或尚有容量。现有 `WorkerHealthReport` 只由调用方传入；直接据此建立 Dispatch 会让旧/伪造内存
状态成为调度 authority。

本切片交付 ARC-04.1c Registry v5 durable health authority，作为 6c2a3 Dispatch/Claim 的最小跨文档前置。

## 后续 Dispatch 必须读取

1. Placement 的 exact worker id/instance/epoch/contract SHA；
2. Registry active pointer；
3. 同 incarnation 最新 durable Health Report；
4. heartbeat 在 dispatch 时间仍为 healthy；
5. `accepting_jobs=true` 且 reported active jobs 未达到合同上限；
6. Registry atomic reservation 的真实 available slot；
7. Target Baseline Resolution 仍 current。

reported active jobs 与 Registry reservations 是不同事实：前者来自 Worker 进程观测，后者是 Runtime 承诺的物理 slot。
后续 Gate 必须同时满足，不能用其中之一代替另一个。

## 明确未授予

本前置不建立 Dispatch artifact、不 reserve capacity、不生成 challenge、不领取 lease、不传输 baseline、不执行测试，
也不授予 result/learning/promotion authority。

后续 `HAR-09.6c2a3b` 现已消费本 durable authority 建立 queued Dispatch 与 atomic capacity reservation；
authenticated claim/lease、传输和执行授权仍未完成。
