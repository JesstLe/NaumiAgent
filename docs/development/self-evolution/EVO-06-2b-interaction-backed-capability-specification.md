# EVO-06.2b Interaction-backed Capability Specification

## 目标

把 EVO-06.2a 中仍为 `needs_specification` 的 Capability Proposal，经由 Harness 持久交互逐步补齐为
可审查的结构化规格。规格是 Candidate revision 绑定、append-only、可恢复的事实快照；它不是 Tool
实现、Registry 注册、Sandbox 准入或执行授权。

本切片坚持每次只推进一个步骤，顺序固定为：

1. `interface`：Tool 名、参数/结果 JSON Schema、错误契约、语义版本；
2. `permissions`：permission family、最小 scope 与理由；
3. `data`：输入/输出数据分类、retention 与敏感数据处理；
4. `verification`：至少一个真实场景 fixture 与机械预期；
5. `operations`：owner、P95 latency、成功率、维护责任与额外退休条件。

## Authority 与身份

- `specification_id` 只绑定 Candidate ID、revision、完整 digest 与 generator version；Portfolio 排名变化会
  形成新 Proposal ID，但不会丢失同一 Candidate revision 已完成的规格步骤。
- 每次进入步骤前重读 `EvolutionReviewService`；只有 source authority、cooldown 和当前 Portfolio 均有效
  时才允许发起交互。
- 用户回答提交到 Harness 后、写入规格前再次重验当前 Proposal。回答期间 Candidate revision 或 authority
  变化时 fail closed，旧答案不会写进新规格。
- 每个 revision 必须绑定唯一的 answered custom interaction ID、sequence、digest 和 answered time；
  option `defer/inspect` 不创建 revision。
- `proposal_ids` 记录同一 Candidate revision 在排名变化期间实际消费过的 Proposal 身份。
- 完成五步仍固定为 `sandbox_eligible=false`、`shadow_eligible=false`、`executable=false`、
  `registry_mutation_allowed=false`；bypass 不改变这些不变量。

## 持久化与恢复

`EvolutionCapabilitySpecificationStore` 与 Candidate Store 共用 evolution SQLite 文件，但使用独立的
`evolution_capability_specifications` append-only 表。主键为 workspace、specification ID 和 revision；
source interaction 另有 workspace 级唯一约束。payload 使用 canonical JSON 与 SHA-256，读取时同时校验
摘要、投影列和 Pydantic 不变量。

如果 UI 已把答案提交到 Harness、进程却在规格写入前崩溃，下一次 `inspect/advance` 会查找同一步骤尚未
对账的 answered interaction，重新验证 JSON 后写入一次。相同 interaction 的并发重试返回同一快照；
若同一步骤出现多个有效未对账答案，系统拒绝猜测。无效旧答案不会阻塞后续新的有效尝试。

## 输入边界

- 自定义答案必须是最长 4000 字符的 JSON object，拒绝疑似 token/password/cookie/API key；
- JSON Schema 最多 12000 字符、256 nodes、8 层，不允许 `$ref/$dynamicRef`；object 明确声明
  `properties`、`required` 和 `additionalProperties=false`；
- workspace scope 只能是安全相对路径/glob；network/browser 只能是域名；process 只能是无参数 executable；
  secrets 只能保存逻辑 credential reference ID，不能保存 secret 值；
- `secrets` permission 与 `credential_reference` data class 必须同时存在；无 credential reference 时
  sensitive handling 必须为 `deny`；
- Tool Catalog miss 已知名称不可被交互改写；Goal need 的未知名称仍必须由用户明确填写，不能从哈希反推。

## 双通道与界面

- 用户 Slash：`/evolution capability-spec <candidate-id>`；每次调用推进一个步骤；
- Agent Tool：`evolution_capability_specification(action='inspect|advance', candidate_id='<id>')`；
- New UI：typed `evolution/review/request` 使用 `action=capability-spec`，复用全局 Harness interaction 面板，
  detail 显示 revision、已完成步骤、下一步、pending interaction 和关闭的执行权限；
- Textual/fallback：Slash 与 Agent Tool 复用 `render_capability_specification()`；
- Node protocol 重算规格 ID、校验连续步骤/未决项/interaction source 与所有 false authority，并只把
  白名单 authority 投影放入前端状态，隐藏字段不会透传。

## 验收证据

小模块测试必须覆盖：

- 五次真实 Harness + SQLite 交互形成 revision 1..5，表中保留五条不可变快照；
- defer 不写 revision，后续 attempt 使用新 interaction ID；
- 非法 Tool 名、secret、schema、scope、跨字段冲突被拒绝且不污染后续尝试；
- answered-before-crash 可对账，重复/并发写入幂等；
- 回答期间撤权不写规格，持久 payload 篡改 fail closed；
- Slash、Agent Tool、typed New UI、fallback renderer 共用同一 service；
- Node 拒绝伪造 `executable=true`、错误未决项或 interaction source，并丢弃私有字段；
- 80/120/200 列 detail 渲染不越界。

## 自我审视与当前不足

本实现没有自动生成 Tool 代码，也没有把用户答案当作可信实现。完整规格只关闭“字段未知”这一道门；
owner 身份、权限合理性、schema 兼容性、真实 fixture 可执行性和 SLO 可达性尚未由独立治理者签署。

下一最小切片为 EVO-06.2c：对 complete specification 建立不可变治理输入、独立校验结果与明确的
approve/reject 决策。只有有效批准快照存在时，EVO-06.3 才能设计临时 namespace、内置 Tool 冲突拒绝、
可撤销 Sandbox Registry 和注册恢复；不得从 `state=complete` 直接跳到 Sandbox。
