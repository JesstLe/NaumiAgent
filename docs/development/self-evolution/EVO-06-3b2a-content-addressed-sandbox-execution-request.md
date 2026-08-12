# EVO-06.3b2a Content-addressed Sandbox Execution Request

## 目标

把 EVO-06.3b1 已机械化的 Capability Scenario Binding 编译为 ARC-04/Harness 可以消费的、
不可变且可动态撤权的执行请求。请求封存“将来要执行什么”，但不代表“现在允许执行”：本切片不
materialize overlay、不启动进程、不签发 Run Grant、不注册候选 Tool，也不进入 Shadow。

这一步是 3b1 与真实隔离执行之间的 authority 边界。若没有该边界，执行器只能临场读取当前源码、
当前 Python、当前 scenario 和当前权限，无法证明执行结果来自已审查的同一份输入。

## 输入与不可变身份

`EvolutionCapabilitySandboxExecutionRequest` 必须同时绑定：

- current `EvolutionCapabilitySpecification` ID/digest；
- current sealed `EvolutionCapabilityImplementationArtifact` ID/digest；
- current `EvolutionCapabilityScenarioBinding` ID/digest；
- permission specification digest 与按 family 排序的精确 scopes；
- 干净 Git 根目录的 exact `HEAD^{commit}` 与完整 `ls-tree` SHA-256；
- 当前受控 Python 解释器绝对路径与固定 driver policy version；
- candidate、driver、逐场景 input 的 UTF-8 overlay 内容和 SHA-256；
- 逐场景 exact argv、timeout、input digest 与 result/error oracle digest。

Request 的 `request_sha256` 对完整 canonical payload 寻址，`request_id` 由摘要派生。SQLite Store
使用 `INSERT OR IGNORE` 保证同一内容幂等，并在每次读取时同时校验持久摘要、Pydantic 严格模型和
Request 自身摘要。overlay 路径只能位于 `.naumi/evolution-sandbox/<artifact-id>/`，不能绝对寻址、
越界或重复，单文件上限 512 KiB，场景数上限 8。

## 确定性执行 Driver

Request 内置版本化 Python driver source，而不是让模型在执行时生成脚本。每个 check 的 argv 固定为：

```text
<python> -I <driver.py> <scenario.json> <candidate.py> <class-name>
```

Driver 在隔离解释器中：

1. 严格读取版本化 scenario envelope；
2. 从 overlay path 临时加载候选类并确认其继承 `Tool`；
3. 以 scenario timeout 调用 `Tool.execute(**arguments)`；
4. 普通成功只接受字符串并 JSON decode；
5. `ToolExecutionError` 只返回稳定 `error_code` 与 `retryable`；
6. timeout 和其他异常降为固定、无 secret 的结构化 envelope。

本切片只验证该 driver 在真实临时 Git fixture 中可运行。3b2b 才能通过 ARC-04 Worker materialize
overlay、施加 OS/permission 约束并把输出与 oracle 比较。

## 动态撤权与并发语义

- `prepare` 前重读 current Binding、Specification 和 Artifact；任一 digest 漂移即 fail closed；
- Git 工作树必须干净、调用目录必须是仓库根目录，编译前后 HEAD/status 必须一致；
- Request 编译后再次重读 Binding 和 Git identity，避免检查与落库之间的 TOCTOU；
- `inspect` 不相信历史 `ready`，每次重算 Binding 与 source currentness；
- 历史 Request 不删除，但来源漂移后 View 变为 `revoked`；
- Store 写入串行化，content-addressed 重试返回同一 Request；持久 payload 被修改时读取失败；
- bypass 不改变 Git、Binding、digest、overlay 或 authority 校验。

## Authority 边界

以下字段永久为 false：

- `network_access_authorized`；
- `sandbox_execution_authorized`；
- `registration_authorized`；
- `shadow_authorized`；
- `executable`。

`permission_observation_required=true` 只说明 3b2b 必须观察并核对候选行为，不能把声明的 permission
当作已授予权限。需要 network/browser/secrets 的候选虽然可形成 Request，但 v1 执行器在对应 capability
存在可信隔离 adapter 前必须拒绝执行，不能退化为宿主机直通。
`runtime_identity_required=true` 同样要求 3b2b 在签发 Run Grant 前解析并封存解释器/平台 identity；3b2a
记录的 Python 路径不是 runtime 已受信任的证明。

## 双通道与 UI

- Slash：`/evolution capability-sandbox <candidate-id>`；
- Agent Tool：`evolution_capability_sandbox_request(action='inspect|prepare', candidate_id=...)`；
- New UI：typed `evolution/review/request` 的 `capability-sandbox` action；成功后显示 Request ID、Git
  revision、场景/overlay 数量和全部 authority 均为“否”；
- fallback/CLI 与 New UI 复用同一个 Service 和 renderer；
- UI 不传输 overlay 内容、scenario arguments 或 oracle 值。

## 验收证据

- [x] 真实临时 Git 仓库、完整 Specification、approved Governance、sealed Artifact 和人工 Binding
  可形成 content-addressed Request；
- [x] candidate/driver/scenario overlays 可按 Request argv 运行真实 result 与声明式 error 场景；
- [x] exact revision/tree、overlay/argv/timeout/oracle/permission digests 被封存；
- [x] Git 漂移动态撤销 Request，Store payload 篡改 fail closed；
- [x] 重复 prepare 幂等，Slash、Agent Tool、typed New UI action 使用同一 Service；
- [x] Ruff、Python 小模块测试、Node command parser 测试和文档治理通过；
- [x] 未以 import smoke、mock-only UI 或未执行 driver 冒充验收。

## 自我审视与下一步

3b2a 证明输入可重放，但尚未证明隔离、permission observation 或 oracle 判定。下一最小切片
EVO-06.3b2b 必须把 Request 转为 ARC-04 ephemeral source snapshot + overlays，签发 exact、短期、
一次性的 Run Grant，在独立 Worker 中逐场景执行，并形成 content-addressed Execution Receipt。Receipt
必须区分 passed、oracle mismatch、declared error mismatch、timeout、permission violation、source drift
和 infrastructure failure；任何失败、缺失场景或来源撤权均不得形成 Registry lease。
