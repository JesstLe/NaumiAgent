# CC-01.1b Source Refresh Approval History

## 目标与边界

本切片把 CC-01.1a 的单点 source identity manifest 升级为受治理的刷新流程：基线导入、变化提案、
人工审批和历史查询共享同一个持久化权威。它不会 fetch remote、修改 source checkout、采纳上游代码，
也不会因检测到新 commit 就自动更新 manifest。

该能力是维护者治理入口，不是 Runtime 中允许 Agent 自主批准许可证或映射变化的 Tool。CC-05 后续只
消费这里的已批准基线和提案事实，不另建第二套 source identity 权威。

## 持久化权威

`SourceRefreshStore` 使用平台原生用户状态目录下的 `claude-source.db`：

- macOS：`~/Library/Application Support/NaumiAgent/claude-source.db`；
- Linux：`$XDG_STATE_HOME/naumi-agent/claude-source.db`，未设置时使用
  `~/.local/state/naumi-agent/claude-source.db`；
- Windows：`%LOCALAPPDATA%\NaumiAgent\claude-source.db`；
- 测试和便携部署可用 `NAUMI_STATE_HOME` 显式覆盖。

它不放入工作区 `.naumi`：审批历史跨工作区、需要长期保留，而且 Agent 在 bypass 下也不应把源身份
审计权威当作普通工作区文件覆盖。Store Catalog 将其登记为 `governance.claude_source`，敏感级别为
`restricted`，保留策略为 `audit_long_term`。

数据库 schema 使用 `PRAGMA user_version = 1`。打开时会校验两张表的完整列契约；未知版本、缺表或
畸形表结构均失败关闭，不会自动覆盖。POSIX 主数据库文件权限收紧为 `0600`。

## 数据与完整性契约

### History Entry

每次批准保存完整但不包含源码正文的 identity manifest，并记录：

- 单调 `revision` 和前一条 `previous_entry_id`；
- manifest 与稳定 identity 的 SHA-256；
- `baseline_import` 或 `refresh_approved` 决策类型；
- 审核者、原因、时间；
- license/mapping 变化是否经过显式确认。

`entry_id` 覆盖上述不可变 payload。读取任何最新记录或历史列表时都会从 revision 1 校验完整 hash
链，并核对 SQLite 投影列与 payload；摘要篡改、中间记录删除、revision 跳跃和投影列漂移都会拒绝
继续使用。

### Refresh Proposal

提案 ID 由 source、已批准基线、候选稳定 identity 和规范化 changes 确定，不包含观察时间。因此同一
变化的重复检查和并发检查只产生一个 pending proposal。提案保存 commit/remote/worktree、许可证和
mapping 摘要，不保存 Git diff、源码文本、README 全文、用户配置或 secret。

审批前会再次用真实 checkout 和项目 mapping 校验候选。基线变化、候选变化、manifest CAS 冲突均
失败关闭；许可证证据变化必须提供 `--allow-license-change`，mapping 摘要变化必须提供
`--allow-mapping-change`。并发提案和审批通过 `BEGIN IMMEDIATE` 收敛到一条历史记录。

## 维护者命令

所有路径都可显式覆盖。首次把已经审核的 CC-01.1a manifest 导入历史：

```bash
python3 -m naumi_agent.claude_source.refresh bootstrap \
  --manifest frontend/terminal-ui/cc-source-map.v2.json \
  --source /Users/lv/Workspace/claude-code \
  --project-root . \
  --reviewed-by maintainer \
  --reason "导入已审核的 source identity 基线"
```

检查当前 source；只有 identity 真正变化时才建立提案：

```bash
python3 -m naumi_agent.claude_source.refresh propose \
  --manifest frontend/terminal-ui/cc-source-map.v2.json \
  --source /Users/lv/Workspace/claude-code \
  --project-root .
```

审批仍然有效的提案并原子更新 manifest：

```bash
python3 -m naumi_agent.claude_source.refresh approve \
  --proposal-id <sha256> \
  --manifest frontend/terminal-ui/cc-source-map.v2.json \
  --source /Users/lv/Workspace/claude-code \
  --project-root . \
  --reviewed-by maintainer \
  --reason "已复核 commit、许可证与 mapping 证据"
```

查询最近审批历史使用 `history --limit 20`。若提案包含许可证或 mapping 变化，审批者还必须在完成
对应人工复核后显式增加确认 flag；flag 不是替代审核的自动豁免。

## 验收证据

- 真实临时 Git checkout 覆盖 clean baseline、commit 变化、dirty/stale 拒绝和 manifest 原子更新；
- 重复 bootstrap/propose/approve 幂等，16 路并发 propose 与 approve 均收敛；
- license 和 mapping 证据变化分别需要显式人工确认；
- source 在提案后再次变化时拒绝审批且不修改 manifest；
- history payload、投影列、链中记录和 schema 版本异常均失败关闭；
- review reason 疑似包含 token/API key 时拒绝持久化；
- 当前本地 Claude Code checkout 通过真实 governance 校验，临时数据库 bootstrap/propose/history
  链路可运行且未改动 source 或用户正式状态。

## 未完成项

CC-01.2/1.3 仍需定义许可证适用范围和逐项 v2 mapping；CC-01.4-1.6 仍需 intake classifier、
provenance 与完整 review gate。CC-05.1 已建立只读 baseline observation/read model，并严格引用本
模块的已批准 history；后续 CC-05 差异模块仍不得绕过审批或复制一套基线存储。
