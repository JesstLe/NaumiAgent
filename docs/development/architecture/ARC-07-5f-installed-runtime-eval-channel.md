# ARC-07.5f Installed Runtime Eval Channel

## 状态

已实现。

## 目标

为 HAR-09.6c2 提供一个不会退化为“导入当前 workspace 源码执行”的 installed-runtime 行为证据通道。
调用方把版本化、内容寻址的 Eval Request 通过 stdin 交给 exact installed backend binary；binary 只运行已登记的
纯函数 runner，并把严格 JSON Response 返回父进程。父进程重新校验 request/response、slot manifest、backend bytes、
执行时间窗口和机械期望后，保存 binary-bound Receipt。

本切片只交付 `protocol_hello@1` transport。它是 HAR-09.6c2 的真实前置，不等于完整 post-rollback behavioral
evaluation，也不授予 Outcome、learning 或 promotion authority。

## 权威链

```text
workspace-declared protocol Suite + digest-locked fixtures
  -> parent-only ReleaseRuntimeEvalRequest (contains expected)
  -> answer-stripped ReleaseRuntimeEvalProcessRequest
  -> exact immutable installed slot/backend SHA-256
  -> backend --runtime-eval-json
  -> ReleaseRuntimeEvalResponse
  -> parent-side expected/actual/time recomputation
  -> ReleaseRuntimeEvalReceipt
```

父进程 Authority Request 固定 Suite 原始 SHA-256、case 顺序、fixture 原始 SHA-256、canonical payload SHA-256、
expected、预算和 `protocol_hello@1` runner version。实际交给被评估 binary 的 Process Request 删除全部 expected，
只保留 payload、预算与 Authority digest，防止 runtime 读取答案后伪造通过。Response 只固定实际协商结果、每 case
耗时和 guardrail，不携带通过聚合。Receipt 再绑定 slot、manifest、binary SHA-256、两种 Request、Response、输入
输出字节数和当前进程时间窗口，并由父进程机械生成通过聚合。

## 进程与安全边界

- hidden backend 入口为 `--runtime-eval-json`，必须同时存在 `NAUMI_RELEASE_RUNTIME_EVAL=1`；
- Process Request stdin 最多 256 KiB，stdout+stderr 最多 512 KiB，最多 100 cases，进程 30 秒硬超时；
- 只接受 `protocol_hello` Suite，不接受命令、路径参数、任意 Python、模型或工具调用；
- payload 限制 16 层/2048 节点/4 KiB 字符串，并拒绝 token、secret、password、cookie、authorization、API key
  等敏感字段进入 Request 或持久 Receipt；
- 子进程工作目录固定 installed bundle，不继承 provider key、Naumi 配置或其他应用 secret；
- 成功时 stderr 必须为空，stdout 必须是单个 strict typed JSON Response；
- binary 看不到 expected，也不能自报 `matched_expected` 或 pass aggregate；父进程用 Authority Request expected、
  actual 与 case budget 重新计算；
- DB 使用参数化 SQL；读取时重新校验 Receipt digest、installed manifest、immutability 和 backend SHA-256；
- 进程失败、协议外输出、时间漂移、slot/bytes 漂移和持久化损坏全部失败关闭。

## 持久化与并发

Release DB 新增 append-only `release_runtime_eval_receipts`。Receipt 同时保存父进程 Authority Request、去答案的
Process Request 与 Response，是内容寻址事实；相同 Receipt 并发写采用
`INSERT OR IGNORE` 后读取并比对，identity 冲突失败。不同真实执行拥有各自时间与耗时，因此可保留多条历史事实，
后续 HAR-09.6c2 必须选择晚于 6c1 Verification 的 fresh Receipt，不能复用旧结果。

## 真实验收

- 当前 `protocol-hello-core` 六个真实 fixture 全部通过 installed runner；
- 真实 hidden CLI 在缺少受控环境时 exit 78，在受控环境中只输出 typed JSON；
- POSIX immutable release bundle 经 install、`--version` boot、exact backend Eval 后保存并重读 Receipt；
- Receipt JSON 篡改被动态识别为 corrupt；未知 slot 在启动进程前拒绝；
- 输入过大、Suite 越界、fixture 越界/摘要漂移、非 `protocol_hello` runner 均失败关闭；
- 定向 Ruff、py_compile 与小模块 pytest 通过，不运行全量测试。

验收同时发现并修复了 `protocol-hello-core` legacy case 的陈旧能力集合：Suite 现在固定当前 16 项合法 Bridge
capability，避免源码协议扩展后旧期望把健康 runtime 误报为失败。

## 下一步

`ARC-07.5g` 已进一步让 exact binary 自报受限 runtime platform/version，并由 slot target/manifest 交叉验证，补齐
原生 H5c identity 的最后一个 transport 缺口。`HAR-09.6c2a` 继续把此 transport 绑定到 6c1 Verification、
Proposal Before/After 的 exact H5c lane 与 fresh
post-rollback Result/Comparison。只有 lane 覆盖满足原 Final Evaluation contract 后，才能把
`behavioral_evaluation_recorded` 置为 true；unsupported runner 必须明确等待适配，不能降级为 workspace import。
