# CC-05.2a Git Tree 与 Mapped Path Structural Diff

## 目标

从 CC-05.1 `SourceBaselineObservation` 出发，使用本地 Git object database 比较已批准 commit 与当前
Claude Code source 树，生成确定性、可校验的结构差异回执。本切片不 fetch remote、不复制源码正文、
不修改 source map，也不自动产生 adopt 决策。

## 权威与输入绑定

`build_source_structural_diff()` 首先调用 CC-05.1 只读 observation，回执必须绑定：

- `baseline_entry_id` 与 `observation_id`；
- 已批准 Git commit 与当前 commit；
- 是否包含未提交工作树；
- source map digest 状态。

只有当前 project 中 mapping 文件摘要与已批准 observation 一致时，才用它将变化的
Claude Code path 反向关联到 area 和 Naumi target path。mapping 陈旧或无法解析时不猜测影响。

## 机械差异语义

- 通过 `git diff --name-status -z --find-renames --find-copies` 识别 added/deleted/modified/renamed/
  copied/type-changed；
- dirty 当前 checkout 以已批准 commit 直接对工作树比较，并补入未跟踪文件；
- NUL-delimited Git 输出、UTF-8 规范相对路径、16 MiB 输出上限和 20,000 条变化上限
  避免换行路径注入和无界资源使用；
- 删除/重命名 mapped path、改动许可证证据或主流 Node 依赖清单产生独立 risk flag；
- 回执对 changes 顺序、counts、mapped impact、risk flag 与整体 SHA-256 做自校验。

## Dirty Baseline 失败关闭

CC-01 历史模型允许经人工说明的 dirty identity，但 Git commit 无法重建当时未提交内容。
CC-05.2a 因此对 dirty approved baseline 返回 `invalid`，不把当时已存在的改动误报为新上游差异。
后续若要支持，必须在 CC-01 增加已批准 tree artifact，不得从 worktree digest 反推内容。

## 维护者命令

```bash
python3 -m naumi_agent.claude_source.structural_diff \
  --manifest frontend/terminal-ui/cc-source-map.v2.json \
  --source /Users/lv/Workspace/claude-code \
  --project-root . \
  --store <NAUMI_STATE_HOME>/claude-source.db
```

`unchanged` 返回 0；`change_detected`/`invalid` 或读取错误返回 1。命令仅输出结构化 JSON。

## 验收证据

- 同一 approved baseline 重复比较得到完全相同的 `diff_id`，Store mtime 不变；
- 真实 Git commit 覆盖 rename/delete/add/modify、mapped path 影响和 dependency risk；
- dirty worktree 覆盖 tracked 修改、untracked 文件与 stale mapping；
- license 变化、缺失 checkout 与 dirty approved baseline 都有不同且事实化的结果；
- 修改 receipt 内容时 digest 校验拒绝；
- 临时已批准 Store 对真实 `/Users/lv/Workspace/claude-code` 运行为 `unchanged`，不改动
  source、manifest 或正式用户状态。

## 未完成边界

CC-05.2b 仍需在本回执之上做 export/component/event/keybinding 符号级差异；CC-05.3 行为
fixture、CC-05.4 impact owner routing、CC-05.5 review decision 和 CC-05.6 map migration 均未实现。
