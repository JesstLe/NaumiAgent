# UI-12.3b2 精确子授权回执

## 目标

把 UI-12.3b1 的外层委托白名单转化为一个不可继续转授、参数精确、短期有效的下游执行授权。父回执本身
不能执行下游 Tool，子回执也不能授权第二代子调用。

## 合同

- Permission Decision Store schema v3 为子回执增加 parent receipt id/digest 与 `expires_at`；v1/v2 原摘要
  保持可验证，数据库在首次异步访问时惰性升级；
- `issue_delegated()` 重新读取父回执，要求父决定允许执行、包含目标 Tool 白名单、绑定非空 run，并且决定
  时间不超过 300 秒；
- 当前只允许 policy、bypass 或 allow-once 父来源；尚不能证明仍有效的 session grant 拒绝派生；
- 子回执继承 parent session/run/agent/permission mode，只保存精确子参数 SHA-256，TTL 为 1–120 秒；
- 子来源固定 `delegated`、actor 固定 Runtime、委托白名单固定为空；任何二次派生 fail closed；
- ARC-04.2a 签发 delegated ExecutionGrant 时再次读取父回执，复核 digest、scope、session/run、子参数与
  expiry，不能只信调用方传入的 child receipt。

## 验收证据

- 真实 parent→child→ExecutionGrant 链签发成功，并保留 delegated source；
- 更改子参数、目标 Tool、父 digest、过期时间或尝试孙授权均拒绝；
- 子授权 retry 按 session/call 幂等复用首次事实；
- v1 与 v2 receipt 均可读取并迁移到 v3，新旧记录无需重写；
- 权限 Store、ExecutionGrant、Catalog 与 Runtime Composition 相关小模块测试通过。

## 边界与下一步

- 本切片自身不注册 Worker、不创建 Tool run lease，也不发送 Shell payload；ARC-04.3b/HAR-08.4b 已作为消费者
  接通该链路；
- Engine 只在实际 Tool 调用的 `ContextVar` 任务作用域绑定精确父回执，Tool 返回或抛错后自动复位；Service
  重新核对 tool/run/arguments digest 与 `bash_run` 委托范围，不接受全局可变“当前权限”；
- session grant 后续调用必须先具有可验证的当前 grant 状态，不能直接放宽本合同。
