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

CC-01 治理先行；CC-02 是 Ink 决策实验，CC-02.4a 已让实验 renderer 复用多 namespace 行高索引做
有界 presentation projection，但性能门仍为 `defer`；CC-03/04 按产品优先级迁入；CC-05.1 已提供只读批准
基线 observation，CC-05.2a 已提供 Git tree 与 mapped path 结构差异，CC-05.2b 又提供 export/component/
event/keybinding 的 typed 符号差异；后续行为 fixture、影响路由与采纳报告仍不得绕过 source refresh
approval。CC-03 的 Naumi Doctor export 产品合同已由 UI-13.5a 提供；
CC-03.1a 已增加绑定 source identity、license scope、source symbol、target symbol 与 target test 的
核心行为清单；CC-03.1b 又以严格 24 格矩阵补齐 Task、Permission、Doctor 的八个行为维度，因此
CC-03.1 已完成但 CC-03 整体仍为 `partial`。CC-04.1a 又把真实 Skill Loader 的 workspace/user/
configured 来源、同名遮蔽与无效 manifest 形成 typed snapshot，并由 `/extensions skills` 和 Agent Tool
共享读取；Plugin/MCP provenance、信任、安装、隔离与管理 UI 尚未完成。protocol 语义迁移和 golden
对齐仍未完成，不得把 CC-03 或 CC-04 标记为 implemented。
