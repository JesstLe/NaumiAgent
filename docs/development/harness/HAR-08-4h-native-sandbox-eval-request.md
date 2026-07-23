# HAR-08.4h Native Sandbox Eval Request Authority

## 目标

为通用 Sandbox Eval 建立第一份 Harness 原生、不可变且可机械复验的执行请求。此前 HAR-08.4e/4f 的
check-group 与 Batch coordinator 只接受 `red|green|adversarial`，普通 Harness 调用没有自己的 lane，也没有
稳定绑定 Profile checks、Git revision、H5a suite/batch 与预算的 authority；直接开放 Slash/Tool 会迫使
Service 临时拼接参数，无法安全恢复。

## Native lane

HAR-08.4e 与 HAR-08.4f 现在接受 Harness 原生 `sandbox` lane：

- 每个 check run ID 使用 `hevalsandbox-*`，并绑定 request authority key、sample index 与 check id；
- 不改变既有 RED/GREEN run ID，adversarial identity 也保持兼容；
- `HarnessSandboxBatchCheckpoint.lane` 可表达 `sandbox`；
- Evolution compatibility coordinator 仍只允许 RED/GREEN/adversarial，不能借 native lane 绕过自身语义。

## Request 编译

`HarnessSandboxEvalRequestBuilder` 只接受：

- 已解析的严格 `HarnessProfile`、精确 SHA-256 Profile digest 与 `profile_trusted=True`；
- 1..80 个按调用顺序排列、ID 唯一且确实存在的 Profile checks；
- 合法 batch ID 与 5..100 个 samples；
- 精确 Git 仓库根目录。

编译器在任何权限、lease、Run Grant 或 Worker admission 前机械执行：

1. 两次读取完整 `HEAD^{commit}` 与工作树状态；
2. 要求 tracked/untracked 均为空，且编译期间 HEAD/状态没有漂移；
3. 对 `git ls-tree -r -z --full-tree <commit>` 原始字节计算与 HAR-08.4c 相同的 tree SHA-256；
4. 为每个 check 冻结完整 spec digest、argv digest、timeout 与顺序；
5. 要求 `sum(check timeout) * samples` 不超过 Profile Eval 总预算，并收敛到 60..3600 秒 coordinator 边界；
6. 从执行过程生成稳定 `harness_sandbox_*` suite identity；batch/source/profile 变化只改变 request identity，
   不会错误改变同一过程的 suite；
7. 对完整 payload 生成 `hseval_*` request ID 与 SHA-256 authority key。

请求同时固定无公网、无依赖安装、精确 Git revision materialization、持续 sample index、H5a Store、
Runtime identity 和执行权限必需等安全事实。`validate_request_checks()` 在真正执行前从当前 Profile 重建 typed
checks，任何 spec/argv/timeout 漂移都 fail closed。

## 验收证据

- 临时真实 Git 仓库完成 init/commit 后，可重复编译同一 request，并与真实 commit 和 raw tree digest 对齐；
- 相同过程、不同 batch 保持 suite ID 相同而 request digest 不同；
- 未信任 Profile、错误 digest、未知/重复 check、非法 batch、布尔 sample 在 Git 读取前稳定拒绝；
- dirty worktree 与非仓库根目录稳定拒绝；
- 最坏 timeout 超预算时不生成 request；
- 编译后 Profile argv 漂移由执行前 resolver 拒绝；
- native sandbox run ID 绑定不同 authority key，完整 H5a 快速返回 checkpoint 可表达 sandbox lane；
- Ruff、编译与相关小模块测试通过，未运行全量测试。

## 当前边界与下一步

HAR-08.4h 只建立可执行请求权威，不签发父权限、Run Grant，不运行 checks，也不写 H5a。v1 为保证恢复语义只
接受干净、已提交的 Git revision；未提交候选应先进入 HAR-08.4d 受摘要保护的 overlay/candidate snapshot，
不能隐式读取易漂移 working tree。下一切片应让 `HarnessService`、`harness_eval_sandbox` Tool 和
HAR-08.4i 已让 `HarnessService` 共用本 request builder、HAR-08.4g admission、4f coordinator、4e kernel
与 H5a Store；HAR-08.4j 已接入 `harness_eval_sandbox` Tool 和 `/harness eval sandbox`。New UI/TUI typed
checkpoint 已由 HAR-08.4k 接线，Eval YAML 的 protocol-only schema 保持不变。跨进程 admission 与真实
queued/cancel/retry 仍待后续权威实现。
