# UI-17.1 Terminal Capability Manifest

## 1. 目标

为默认 New UI 与 Textual TUI 建立独立、随产物发布、可机读且 fail-closed 的产品能力声明。它回答“这个表面承诺哪些
核心用户能力、使用什么协议、证据在哪里”，不把 JSONL negotiation capability 与完整产品 parity 混为一谈。

本切片不把 manifest 加入 hello 握手，不实现跨版本降级，也不执行 UI-17.2 golden scenario；这些分别属于 UI-17.3
和 UI-17.2。

## 2. 清单合同

两个权威文件：

- `frontend/terminal-ui/capability-manifest.json`：New UI，声明 `jsonl`、协议 1、支持协商；
- `src/naumi_agent/tui/capability-manifest.json`：TUI，声明 `in_process`、语义版本 1、不伪造协商。

共享 Python schema 固定 14 项发布必需能力：会话提交/流式、工具生命周期、权限/bypass、取消、Agent/Task、Goal/Pursuit、
Harness receipt/explain、history/resume、Doctor/debug、model/provider、budget/context、排队立即发送、用户交互和 terminal
runtime health。每项必须声明 `supported/degraded/unsupported`，并提供 2-8 条受限仓库相对证据；非 supported 必须解释。

缺项、未知项、重复/越界证据、目录穿越、surface 错配、schema 不兼容、in-process 伪协商或任一必需能力非 supported 都
拒绝通过。校验错误只输出稳定原因，不回显原始 JSON 内容。

## 3. 发布与验收证据

- New UI 与 TUI manifest 都被 wheel 显式包含，源码测试目录不进入产物；
- 隔离构建真实 wheel 后，两份 manifest 均存在；经 `pip --target --no-deps` 解包安装后，默认 resolver 可读取两端；
- New UI 声明的 min/max 与 Python JSONL protocol 常量一致；
- 两端 14/14 必需能力状态为 supported，coverage 集合逐项完全相同；
- 每条 evidence 在当前 checkout 中都是真实文件，缺失路径可机械报告；
- 5 类非法 manifest、surface 错配、错误 parity 输入和缺失 evidence 均有定向测试；
- Ruff、compile、JSON/pytest 与 wheel 配置定向测试通过，未运行全量测试。

## 4. 自我审视与下一步

manifest 证明“产品声明有结构、有证据文件”，尚不能证明两个表面在同一输入下产生完全相同语义；UI-17.2 必须使用
同一 fixture 对 submit、tool、permission、interaction、receipt、cancel、runtime health 等关键字段做 golden 对照。
manifest 目前也没有 digest/版本进入 New UI hello，UI-17.3 才能据此协商降级或拒绝。不能把本切片标记为完整 UI-17。
与仓库现有 protocol asset loader 相同，默认 resolver 面向标准解包安装，不支持把 wheel 直接作为 zip 放进 `PYTHONPATH`；
常规 pip/uv/pipx 安装不受影响，若未来明确支持 zipapp，应统一迁移所有 frontend resource loader。
