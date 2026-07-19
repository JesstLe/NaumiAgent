# UI-17.2b Permission 与 Interaction Golden Scenarios

## 1. 目标

锁定 New UI Bridge 与 Textual TUI 的两组安全关键控制语义：工具权限确认和模型主动询问。两端必须从同一 Python
投影器生成公开请求，Node New UI 必须按同一 fixture 保留请求、选择与终态含义。本切片消费 UI-12 权限链和
HAR-10.6/UI-18.4 durable interaction authority，不重建授权或问题持久化状态机。

## 2. 权限公开合同

权威 fixture 是 `tests/fixtures/ui17/permission-interaction-golden.json`。公开的 `permission/request`：

- 只显示有界 `arguments_summary`，authorization、token 等私密值必须替换为 `[已隐藏]`；TUI Modal 禁止读取原始
  `arguments`；
- 后端仅可声明 `allow_once`、`deny`、`grant_session`，缺失、未知或类型错误均在弹窗前 fail closed；
- terminal 公开选择额外包含全局 `bypass`，它不是后端临时授予的工具权限；
- `bypass` 表示常规工具全权限通过，不再对高风险请求增加二次确认，公开字段固定
  `requires_double_confirm=false`；系统不可破坏边界、资源限额和审计仍由底层安全链执行；
- 四个选择的终态分别为 `allowed`、`denied`、`granted`、`bypass_enabled`，TUI 返回 canonical choice，不再返回
  旧值 `allow`。

## 3. 模型主动询问合同

`public_interaction_request_payload()` 是两端共同的公开投影器，固定 request/session/run/agent、问题、选项、自定义输入、
deadline 与 `needs_input` 状态。无论是否存在 durable authority，TUI 和 Bridge 返回的答案都必须经过同一个规范化器：

- 选项答案补全稳定的 value、label 和空 custom_text；
- 自定义答案使用空 value、custom label 和有界 custom_text；
- 非法组合由现有协议校验拒绝，不能把前端原始对象直接交回模型；
- durable authority 仍负责 create/lease/answer/expire/takeover，本切片只统一其前后公开边界。

## 4. 验收证据

- Python shared projector、真实 Bridge callback 和真实 TUI callback 对同一权限请求逐字段相等；
- 权限 fixture 中的 token 不出现在公开 JSON，TUI 四个按钮返回 canonical choice，非法 choices 不打开 Modal；
- Bridge/TUI 对同一 interaction 生成相同公开请求，并返回相同规范答案；
- Node reducer 对四个权限终态逐项验证，键盘选项与自定义输入产生 fixture 指定的 client payload；
- 权限/interaction 相关 Python 定向子集 75 项与 Node 定向子集 4 项通过；Ruff、compile、JSON 与 diff check 通过；
  未运行全量测试。

## 5. 自我审视与下一步

本切片证明的是公开语义一致，不代表 UI-12 pending queue、断线恢复或 UI-17.2 全部完成。颜色、布局和 Modal 动画不进入
golden，但两端必须保留文字标签，不能只靠颜色传达授权结果。下一独立切片应覆盖 submit/stream、tool lifecycle、completion
receipt 与 cancel；之后才能评估 UI-17.3 compatibility negotiation 的最小前置。
