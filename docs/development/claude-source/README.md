# Claude Code 源码对齐模块册

本项目当前引用的本地研究源位于 `/Users/lv/Workspace/claude-code`；机器映射位于
`frontend/terminal-ui/cc-source-map.json`，v2 source identity 位于同目录
`cc-source-map.v2.json`，受限 license scope 位于 `cc-license-scope.v1.json`。任何复用必须先重新核对来源版本、许可证声明和实际文件，不得只引用
2026-06-02 的旧映射。刷新提案与审批历史由平台原生用户状态目录中的 `claude-source.db` 管理，
详见 `CC-01-1b-source-refresh-history.md`。

## 原则

- 优先迁移机制、状态机、测试用例和交互语义，不盲目复制组件树。
- Python Runtime、PermissionChecker、TaskStore、Harness 是 NaumiAgent 权威。
- 任何复制代码必须记录 source commit、source path、target path、改动和许可证依据。
- 当前 Claude scope 只允许 reference/reimplement；copy/adapt 在人工法律复核前失败关闭。
- CC 源更新不能自动覆盖 NaumiAgent 本地行为；必须通过行为契约和人工审核。

## 模块顺序

CC-01 治理先行；CC-02 是 Ink 决策实验；CC-03/04 按产品优先级迁入；CC-05.1 已提供只读批准
基线 observation，CC-05.2a 已提供 Git tree 与 mapped path 结构差异；后续符号/行为差异仍不得
绕过 source refresh approval。CC-03 的 Naumi Doctor export 产品合同已由 UI-13.5a 提供，但 source
behavior inventory、语义迁移和 golden 对齐尚未开始，不得因此把 CC-03 标记为完成或 partial。
