# CC-05 上游差异监控与行为回归

## 目标

在人工触发下比较本地 Claude Code source baseline 的新 commit，识别路径、接口和行为变化，
生成更新建议但不自动合并代码。

## 子模块

- CC-05.1 Baseline store：commit、map schema、审计结果、许可证 digest。
- CC-05.2 Structural diff：新增/删除/移动文件、export、组件/事件/键位变化。
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
