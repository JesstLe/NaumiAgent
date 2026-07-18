# CC-01.1a Source Identity Manifest

## 目标与边界

本切片把本地 Claude Code 研究源从“文档里写了一个绝对路径”升级为可重复校验的 v2 身份
manifest。它只证明当前审计基线是谁、是否变化和许可证证据是否仍成立；不复制源码、不自动采纳
组件，也不把本地 source checkout 作为 Naumi Runtime 依赖。

## 身份事实

`frontend/terminal-ui/cc-source-map.v2.json` 严格记录：

- 完整 source commit、origin remote、branch、upstream 与 ahead/behind；
- clean/dirty 状态；dirty 时必须同时提供原因和 worktree SHA-256；
- README 许可证证据的相对路径、完整文件 SHA-256 和已审核声明；
- v1 source map 的相对路径、完整摘要及一个发布周期只读兼容策略。

manifest 只保存摘要，不保存 Git diff、未跟踪文件内容、README 全文、用户配置或 secret。
checkout 位置使用可移植 hint，实际校验路径由操作者显式提供。

## 校验语义

`python -m naumi_agent.claude_source.governance` 读取严格 Pydantic v2 契约并返回：

- `valid`：source commit/remote/worktree、许可证证据与 v1 map 均一致；
- `stale`：commit、remote、worktree、许可证文件摘要或 v1 map 发生变化，必须重新审核；
- `invalid`：checkout、许可证声明或 manifest 路径信任边界不可成立。

dirty checkout 不允许静默生成基线；捕获时必须写明原因。worktree digest 覆盖 tracked diff、状态清单
和未跟踪文件内容摘要，但任何原始内容都不会写入 manifest。

## 验收证据

- 临时 Git 仓库真实执行 clean capture、原子写入、严格读取与 valid 校验；
- 新 commit 与 v1 map 变化会机械返回 stale；
- dirty checkout 缺少理由失败关闭，tracked/untracked 内容变化会改变摘要；
- README 中许可证声明缺失时拒绝捕获；
- 未声明字段和 dirty 字段不一致被严格模型拒绝；
- 当前 `/Users/lv/Workspace/claude-code` 在 commit
  `b9c3fb6c2e15476d77f28557e70f2f6dd48bee65` 上通过真实校验。

## 未完成项

CC-01.1b 才建立刷新审批与历史 manifest；CC-01.2/1.3 继续完成许可证适用范围和每条 mapping 的
v2 schema；CC-01.4-1.6 才提供 intake classifier、provenance 和完整 review gate。v1 map 当前仍是
映射内容权威，不能因 v2 identity valid 就宣称组件已经完成采纳审核。
