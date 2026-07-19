# UI-12.3b1 Direct-Allow 回执与委托范围

## 目标

让无需弹窗的 policy/bypass 允许也在受委托工具执行前形成可验证持久事实，并明确该外层工具最多可以派生
哪些下游工具调用。该切片只冻结父授权，不签发子授权。

## 合同

- Permission Decision Store 升级为 schema v2；旧 v1 receipt 不改写即可读取，首次异步写入时只迁移数据库
  版本；
- 新增 `policy_allowed` 与 Runtime actor；bypass 后续直接调用由 Runtime 记录，仍保留 bypass 来源与模式；
- `ToolMetadata.delegated_tool_names` 是排序、唯一、最多 16 项的显式白名单；空白名单工具不增加持久化成本；
- `harness_run_check` 只声明可委托 `bash_run`，不能派生 Browser、Agent 或任意其他 Tool；
- direct allow 回执在工具执行前持久化失败时 fail closed；回执只保存参数 SHA-256 和工具名白名单，不保存
  command、Profile 内容、reason 正文或 secret；
- ARC-04.2a 的 policy ExecutionGrant 现在只接受匹配 `policy_allowed` 回执，不再接受无来源字符串。

## 验收证据

- policy 与 bypass 调用均记录 session/run/call/mode/source/Runtime actor 和 `bash_run` 委托范围；
- policy ExecutionGrant 消费真实 policy receipt 后签发，参数或来源不匹配仍拒绝；
- v1 receipt 摘要保持可验证，Store 惰性迁移到 v2 后新旧记录可并存；
- 委托白名单乱序、重复、超限或非法标识符在持久化前拒绝；
- 权限、ExecutionGrant、Engine、Catalog 与 Runtime Composition 小模块验证通过。

## 后续状态

- UI-12.3b2 已实现独立子回执，绑定父 receipt id/digest、精确 Shell spec 参数摘要、同一 session/run 与短期
  有效期，并禁止递归委托；详见 `UI-12-3b2-exact-child-authorization.md`；
- session grant 的后续调用、Hook/plan block、跨会话查询、retention/export 仍属于 UI-12.3b 后续；
- `/harness check` 尚未切换 Sandbox Runner；下一步仍需 Worker registration/lease/grant/ToolJob admission 组合。
