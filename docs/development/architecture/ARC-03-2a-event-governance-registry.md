# ARC-03.2a 全事件治理注册表

## 目标

为新 UI 与 Python Bridge 当前发布的每一种 JSONL 事件建立单一、可发布、可查询的治理清单，阻止事件新增后只改一端而无人负责的协议漂移。

本切片是 ARC-03.2 的最小前置，不等同于完整 Schema Registry。它先固定事件边界和治理义务，为后续 payload JSON Schema、兼容策略、顺序恢复、类型生成与持久化拦截提供机器可读输入。

## 发布载体

治理清单位于 `frontend/terminal-ui/protocol-contract.json` 的 `event_registry`：

- `client` 必须精确覆盖全部客户端事件；
- `server` 必须精确覆盖全部服务端事件；
- contract 已作为终端 UI 运行时资产打入 wheel，不另建第二份配置；
- Python Bridge 启动时计算注册表规范化 SHA-256；
- `ready` 与 `runtime/status` 发布 contract 版本、摘要和双向事件数量；
- 新 UI `/debug` 显示缩短后的摘要，便于定位前后端是否读取同一份注册表。
- UI-11.1a 新增的 `tasks/snapshot` 同样登记 owner、敏感字段与 snapshot 持久化边界；新增后
  Python/Node 事件数量和注册表摘要同步变化，旧前端会在握手或事件校验阶段明确拒绝漂移。

## 每事件治理字段

每种事件必须且只能包含以下字段：

| 字段 | 作用 | 允许值 |
| --- | --- | --- |
| `owner` | 责任域 | protocol/runtime/harness/inspector/agents/safety/workbench/diagnostics/sessions/tasks/ui |
| `stability` | 兼容承诺 | stable/experimental/deprecated |
| `criticality` | 丢失或未知时的影响 | informational/control/terminal |
| `persistence` | 允许进入的持久化类别 | never/timeline/snapshot/audit |
| `sensitive_fields` | payload 内需要敏感处理的字段路径 | 唯一的 `payload.*` 路径数组 |
| `redaction` | 敏感字段处理义务 | none/required |

`redaction: required` 表示消费者和持久化层必须按后续 ARC-03.2b/3.7 规则处理，不表示本切片已经对用户正文做了不可逆遮蔽。UI 实时展示与持久化副本需要不同策略，不能用统一字符串替换冒充安全治理。

## 运行时约束

### Python

`load_protocol_event_registry()` 负责：

1. 定位源码或安装包中的发布 contract；
2. 对 Python `ClientEventType`、`ServerEventType` 做精确集合覆盖；
3. 严格拒绝未知字段、非法枚举、重复或非法敏感路径；
4. 拒绝“有敏感字段但不要求 redaction”和“无敏感字段却声明 required”；
5. 返回不可修改的事件策略对象，并生成确定性摘要；
6. Bridge 启动时加载一次，失败即阻止带着漂移协议继续运行。

### Node

终端 UI 加载 contract 时执行同等语义校验：

1. client/server 与发布事件数组精确一致；
2. 每个策略恰好六个字段；
3. 枚举、敏感路径、重复项和 redaction 关系合法；
4. `eventPolicy(direction, type)` 只返回注册事件的策略副本；
5. Node 对同一 registry 计算确定性 SHA-256，并严格比较 Bridge 发布的版本、摘要与双向事件数量；仅校验摘要格式不算通过。

## 验收标准

- 当前全部 client/server 事件在注册表中恰好出现一次；
- Python 和 Node 都拒绝缺失事件、未知事件和多余策略字段；
- 敏感字段路径必须唯一且以 `payload.` 开头；
- 敏感字段与 redaction 声明不一致时，两端都拒绝启动或接收；
- Bridge 的 `ready`/`runtime/status` 能查询版本、摘要和事件数量；
- 新 UI 收到的版本、摘要或事件数量与内置 contract 不一致时明确拒绝，不带着漂移协议继续运行；
- `/debug` 能让用户看到当前注册表版本和摘要前缀；
- 源码运行与 wheel 安装都读取同一发布资产；
- 只运行协议注册表、Bridge 状态、Node 协议和 wheel 资产定向测试。

## 明确未完成

以下内容不属于 3.2a，仍不得把 ARC-03 标为完成：

- 每事件完整 payload JSON Schema 和跨版本 fixture；
- required/optional 字段兼容矩阵与未知关键事件处理；
- sequence、cursor、去重和 gap snapshot 恢复；
- Python/TypeScript 类型代码生成；
- 持久化前按字段策略执行 redaction/拒绝并生成审计证据；
- 对 raw secret、private reasoning、Harness artifact 的端到端泄漏检测。

建议下一切片为 ARC-03.2b：先选择权限、interaction、Harness receipt 三类高风险事件，建立 payload schema 与持久化边界执行器，再扩展到全部事件。
