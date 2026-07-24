# CC-05 上游差异监控与行为回归

## 目标

在人工触发下比较本地 Claude Code source baseline 的新 commit，识别路径、接口和行为变化，
生成更新建议但不自动合并代码。

## 子模块

- CC-05.1 Baseline observation：从 CC-01.1b 已批准 history 派生 commit、map schema、审计结果、
  许可证 digest 的只读比较基线；不得另建第二套身份权威。
- CC-05.2 Structural diff：已完成 05.2a/05.2b；[CC-05.2a](CC-05-2a-git-tree-structural-diff.md)
  交付只读 Git tree/path diff、rename/copy 识别、mapped path 影响和 license/dependency risk；
  [CC-05.2b](CC-05-2b-symbol-contract-diff.md) 在同一 approved authority 上比较 export、组件、事件与键位
  符号，并以 typed digest receipt 区分 added/removed/modified/moved。
- CC-05.3 Behavioral diff：从 mapped fixtures 比较交互状态机和错误路径。
- CC-05.4 Impact routing：关联 CC/UI/ARC 模块 owner 和测试。
- CC-05.5 Review report：adopt/defer/ignore/security_review，每项有证据。
- CC-05.6 Map migration：审核后更新 source map 与 divergence log。

## 验收标准

- 无 source commit 变化时结果幂等且不制造报告噪声。
- 删除 mapped path 必须标红并列出受影响 target/tests。
- 许可证变化阻断 copy/adapt 更新，等待人工法律/项目决策。
- 上游新依赖的包体、安全和 Node floor 单独评估。
- monitor 不写 source repo、不 fetch 未授权 remote、不修改 Naumi 代码。

## 当前依赖事实

CC-01.1b 已提供受版本治理的 `claude-source.db`、稳定 refresh proposal 和人工审批历史。CC-05.1
已在此基础上实现只读 baseline read model 与确定性 observation receipt；任何候选基线更新仍必须
回到 CC-01.1b 审批，监控自身无权推进 manifest。详见
`CC-05-1-baseline-observation.md`。

## 实现进度

- `CC-05.1`（2026-07-23）已完成：只读加载最新已批准 history，绑定当前 manifest，输出
  current/change_detected/invalid observation；如实标记 legacy mapping 无显式 schema version。
- `CC-05.2a`（2026-07-23）已完成：从 observation 绑定的 approved commit 产生确定性 Git tree
  structural diff，并对 dirty approved baseline 失败关闭。
- `CC-05.2b`（2026-07-24）已完成：从 approved Git blob 与当前 commit/worktree 提取有界 mapped symbol，
  识别公开导出、组件、事件与键位合同变化；mapping 漂移、缺失 baseline 和 parser/容量错误失败关闭。
- CC-05.3-05.6 尚未实现，因此 CC-05 保持 `partial`。
