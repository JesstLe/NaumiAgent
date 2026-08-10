# ARC-07.5h Target Baseline Resolution

## 状态

已实现。

## 目标

为 HAR-09 回滚后跨平台行为复验解析 exact target-specific release artifact。解析结果必须与本机 active
baseline 共享完整 version、source commit 和 source tree，并由当前 Release Channel 与 Build 双信任根共同证明。
仅版本字符串相同不能建立 authority。

## Production 组合

Engine 延迟组合以下安装级 authority；启动本身不要求这些文件存在，只有显式解析时才失败关闭：

```text
<release-root>/
  trust/trusted-builders.json
  trust/trusted-channels.json
  state/release-channel-catalog.db
```

Channel Trust Policy 与 Build Trust Policy 均使用 bounded、no-symlink、identity-stable loader。Catalog Store
重新验证 current latest catalog、有效窗口、Ed25519 channel signature、builder key generation/revocation、Build
Attestation 和固定 archive origin。

## 等价关系

Target Baseline Resolution 同时要求：

1. target 等于 Remote Lane Placement 从 exact Worker OS/CPU 导出的 target；
2. Catalog Entry 与 Build Attestation 的 target/version projection 一致；
3. target artifact version 等于本机 Coverage baseline version；
4. Build Attestation source commit 等于本机 baseline source commit；
5. Build Attestation source tree SHA-256 等于本机 baseline source tree SHA-256。

目标平台 archive、manifest 和 attestation identity 可以且应当不同；它们证明的是同一源码的目标平台构建，不能拿本机
archive 冒充 Windows/Linux artifact。

## 权威边界

Artifact 冻结 Coverage、Outcome、Placement、Worker incarnation、本机 baseline、Release Channel Resolution、
target Build Attestation 和 content-addressed identity。动态 View 每次重新验证 durable source、Placement authority、
current Catalog/trust policy 和 source equivalence。

有效 View 只提供 `download_input_authority=true`，供后续受限 Artifact Fetch 消费；固定保持未下载、未安装、未下发，
execution/result/learning/promotion authority 全部为 false。Channel 或 builder 撤销、Catalog 更新、Placement 失效和 durable
篡改都会撤销解析权威。

## 双通道

- Agent Tool：`evolution_post_rollback_target_baseline(request_id, comparison_id, channel)`；
- 共享 Slash：`/evolution outcome-resolve-behavior <request-id> <comparison-id> <channel>`；
- CLI、Textual TUI 与 New UI 复用同一 Slash Router；
- normal 与 bypass 均无二次确认；lockdown 仍由 PermissionChecker 控制。

## 验收证据

- 真实 Ed25519 Channel/Build signer 与 target-specific archive/manifest 完成解析；
- Windows Worker Placement 只能解析 `windows-x64` entry；
- 同 version 但 source commit/tree 不同会失败关闭；
- Channel key 动态撤销后，既有 Resolution 同步失去 authority；
- 重复 Service、Tool 与 Slash 收敛到同一 durable artifact；
- 相关小模块测试、Ruff、py_compile、文档治理和 diff check 通过；未运行全量测试。

## 后续

`HAR-09.6c2a3` 将先消费本 Resolution 的 current authority，再验证 Worker fresh health、接受任务状态、资源余量并签发
fenced claim/lease；此文档不提前授予远端执行权。
