# EVO-03.1b Validation Profile Check Binding

## 目标

把 EVO-03.1a Validation Plan 中抽象的 lint、compile、unit、contract、smoke 要求，绑定到当前工作区
Harness Profile 中由用户信任、与具体 changed path 匹配且唯一提供对应 capability 的 check。

本切片生成不可变 Binding artifact，不执行 check，不保存 argv 正文，也不把 Profile trust 转换为命令执行
权限。

## Profile capability

`HarnessCheckSpec` 新增可选 `provides`：

- `lint`
- `compile`
- `unit`
- `contract`
- `smoke`

capability 必须唯一并规范排序。旧 Profile 没有 `provides` 时仍可加载并用于原 Harness completion，但不能
冒充 EVO-03 verifier。仓库 Profile 已为 Harness、Evolution、Python compile 与 Terminal UI 的现有检查
声明实际 capability，没有发明新的自由命令入口。

## 可信绑定流程

`EvolutionValidationProfileBinder.bind()`：

1. 重新解析并验证 Validation Plan 摘要；
2. 从目标 workspace 重新加载 `.naumi/harness.yaml`；
3. 要求当前原始 Profile SHA-256 等于 Mutation Source Snapshot 保存的 digest；
4. 从用户级、工作区外 `HarnessTrustStore` 读取当前信任记录；
5. 对每个 `(changed path, required check kind)` 使用共享 Harness changed-path 选择语义；
6. 要求恰好一个 `required_for: change` check 提供该 capability；
7. 生成 Binding，保存 check ID、spec SHA-256、argv SHA-256、timeout 和 capability，不保存 argv。

失信、撤销信任、Profile 漂移、缺失 capability 或同一路径 capability 歧义全部 fail-closed。多文件计划按
路径分别绑定，因此不同模块各自拥有 lint/unit check 不会被误判为全局歧义。

## Glob 语义修复

本切片同时修复共享 `when_changed` matcher：旧 `PurePosixPath.match` 无法让 `src/**/*.py` 匹配
`src/naumi_agent/ui/footer.py`。新 matcher 按路径 segment 解释：

- `**` 匹配零个或多个 segment；
- `*`、`?` 与字符类只匹配单个 segment；
- Windows 反斜杠先规范为 `/`；
- pattern 仍是工作区相对路径。

修复同时服务原 Harness required-check 选择与 EVO-03 Binder，避免两套 glob 规则漂移。

## Binding artifact

`EvolutionValidationProfileBinding` 绑定：

- Validation Plan ID/digest；
- Profile digest、相对路径、trust timestamp/source；
- 每个被使用 check 的 spec/argv digest；
- 每个 path/check-kind 到唯一 check ID 的 coverage；
- Plan requirement digest 与完整 Binding canonical digest。

固定安全状态：

- `profile_trusted=true` 只表示生成时事实；
- `profile_trust_must_be_revalidated=true`；
- `arc04_worker_required=true`；
- `execution_ready=false`。

因此信任随后被撤销时，旧 Binding 也不能被 Runner 直接执行。

## 验收证据

- 未信任 Profile 不生成 Binding，用户信任后相同输入得到确定性 artifact；
- Profile 一字节漂移或信任撤销立即拒绝；
- Python 文件逐路径绑定 lint/compile/unit/contract，unit 与 contract 可由同一 check 真实提供；
- 缺失 capability 与同一路径重复 capability 分别返回稳定错误码；
- Binding 不含 argv/源码/绝对路径，嵌套 digest 篡改被拒绝；
- 深层 `src/**/*.py` changed path 进入共享 Harness required checks；
- Binder 生成前后隔离 worktree 状态不变。

## 当前不足与下一步

- Binding 仍不是执行许可；ARC-04 worker、进程组回收与资源隔离未完成；
- 尚未捕获 baseline/candidate cohort，也没有 HAR-08 Comparison Receipt；
- Profile trust 必须在每次实际执行前再次检查，不能只依赖 artifact 中的历史 `true`；
- 下一切片应先实现 EVO-03.2a Baseline Cohort Request：只描述 HAR-08 runner、fixture、sample、预算和
  Binding 引用，不自行调用 subprocess；待 ARC-04 可用后再接真实执行 adapter。
