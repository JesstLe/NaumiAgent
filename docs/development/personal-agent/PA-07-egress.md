# PA-07：确切外发提案、人工审批与单次执行

日期：2026-11-06

状态：**partial；本切片实现、集中回归及实际模型/公网/生产UI闭环已通过**。
本记录描述 PA-07 后续的审批化外发合同；冷读、旧reader边界、副本回滚和Git
采用本轮独立事务收据，不沿用上一切片的通过结果。完整研究能力仍保留第9节缺口。

前一研究切片 `c3c6187a7f8cea65bb1d6f1151b96a4201772eec` 已有真实来源、
真实模型纠正比例、认证 HTTP、实际浏览器和独立冷读证据，见 [PA-07](PA-07.md)。
那次验收确认确定性研究链路，不证明本切片的人工批准已经运行通过。

完整 24 项视频目标保持原范围。研究仍属于 Agent 可以承担的任务，
本模块不创建预置职业、不改变持久 Agent 身份，也不创建第二套执行 Goal/Job/Run。

## 1. 本轮实现路径

```text
Agent 提交确切查询或网址
          ↓
本地保存提案、来源、接收方与有效期
          ↓
用户查看完整外发内容和接收方
          ↓
人工批准 / 拒绝 / 撤销
          ↓
按原批准执行一次，保存发送准入和原结果
          ↓
读取原收据；已捕获结果可以显式继续保存，结果未知时不自动重发
```

人工批准只针对一份提案的固定内容，不形成会话级、Agent 级或任意网络目的地的通行授权。
原 `search/capture/recheck` 模型入口在私有上下文中的外发规则保持原样；
批准后的执行另走持久提案的精确准入路径，不伪造 `trusted_channel="web"`。

### 主要组件

| 组件 | 责任 |
|---|---|
| `personal/research_egress_models.py` | 严格操作输入、动作字段互斥、不可变批准摘要 |
| `personal/research_egress.py` | 提案、人工决策、作用域、发送准入、原请求与恢复 |
| `personal/research_store.py` | 原研究 Journal 内的 egress / append-only decision、状态单调与身份保护 |
| `personal/research_capture.py` | 无网络的接收方计划生成、计划重核、固定单一服务的生产传输 |
| `personal/research.py` | 复用原 capture/search 意图与来源落库，原子完成外发收据 |
| `personal/research_authority.py` | 带原 Artifact/Memory 依据的确切材料及 authority 7 读回 |
| `personal/research_tools.py` / `research_commands.py` | Agent 与人工入口的不同权限，同一底层执行合同 |
| `api/routes/research.py` | 认证 HTTP、no-store、严格请求和原请求查询 |
| `frontend/shared/src/api/personalResearchEgress.ts` | 共享 DTO 与 API 合同 |
| `frontend/web2/src/personal/research/ResearchEgress.tsx`、`useResearchEgress.ts` | 提案列表、完整核对、批准/执行及原请求恢复 |

## 2. 受支持的确切动作

提案输入共用 `notebook_id`、`expected_notebook_revision`、`request_id`、
`reason` 和 `expires_in_minutes`；用途最长 400 字，有效期 10～60 分钟，默认 30 分钟。

| action | 动作字段 | 固定内容 |
|---|---|---|
| `search` | `query`、`provider`、`max_results` | query 最长 400 字/50 词；DuckDuckGo 或 Brave；1～10 条结果 |
| `capture` | `url`、`max_age_days` | 规范后的完整网址与来源有效期 |
| `recheck` | `source_id`、`expected_source_revision` | 原来源、原版本、从原记录取出的确切网址与有效期 |

动作字段互斥；例如 search 提案不接收 capture 的 url 或调用者自带 context refs。
模型也没有传入调用身份、人工批准标志、可信入口或任意授权 token 的字段。
未指定搜索 provider 时固定为 DuckDuckGo，而不是在执行时按新配置自动挑选接收方。

提案阶段只进行本地规范化、来源授权和配置核对，不执行目标 DNS 或 HTTP。
重定向、正文和查询的实际网络动作都在人工批准后的发送准入之后发生。

## 3. 接收方计划

批准摘要绑定的 `destination_plan` 包括：

- `endpoint`、`method`、`parameters`、公开固定 headers；
- `provider`、请求编码和相关配置的 `routing_digest`；
- `redirect_policy="none"`、`max_redirects=0`；
- `trust_env=false`、`tls_verify=true`；
- Brave 使用的凭据引用和凭据摘要，不包含凭据正文。

审批化搜索只使用原计划中的一个直接服务：

- DuckDuckGo：`POST https://lite.duckduckgo.com/lite/`，固定 form 参数；
- Brave：`GET https://api.search.brave.com/res/v1/web/search`，固定 query 参数。

原计划不自动回退到其他搜索商或浏览器，也不继承环境代理。配置、凭据、
query、目标网址或接收方计划变化后，原批准不再用于新内容。

审批化 capture/recheck 禁止自动跟随重定向，遇到重定向时保存真实来源不可用观察；
新的目标需要另行提出明确提案。原显式人工抓取的既有重定向合同与本路径分开。
生产传输仍检查全部 DNS 地址、固定已验证公网地址、使用原主机 Host/SNI 和 TLS 校验，
限制字节与字符数，拒绝不受支持的编码或正文类型。

## 4. 作用域、批准摘要与来源

提案使用 `rx_<32hex>` 标识。不可变 core 包括：

- workspace、实际 actor ID、当时的 actor version/name 与原 Session；
- 原 notebook ID、revision、title、digest 与确切来源/上游研究基线；
- action、规范 payload、destination plan、用途、创建时间和有效期；
- 原 ArtifactRefs、MemoryRefs、记忆事项范围及输出上下文；
- 原稳定网络意图 request ID。

人工审批核对 `expected_revision` 和 `expected_digest`。`egress_decision` 为 append-only
事实；原批准凭据和开始执行的 intent/holder/fence 不被后续操作替换。
模型只有提案、读取、状态查询及执行已批准提案的能力，没有人工 decide 能力。

### 原始依赖与输出依赖分开

提案捕获真实 Session pins、运行态隐式 ArtifactRefs、Memory ArtifactRefs、
MemoryRefs 与事项记忆范围。隐式资料也固定到真实原 Session；
输出上下文复用既有的 own-notebook context mirror，保留原字节、上游授权和非自身凭据。

- **发送前**：重核原始依赖、当前 notebook/source 基线、actor version、原 Session 和接收方计划。
- **结果读回**：保留原 Artifact/Memory 的活授权；确切历史结果不因自己更新了 source head 而自失效。
- **后来新增 Session pin**：不会改写已经冻结的外发正文；新外发内容仍需另一份明确提案。

人工入口使用 `owner` 请求命名空间，可以核对本工作区不同 Agent 的提案。
模型请求使用实际 actor 命名空间，只访问自己的提案，执行还要求原提案 Session。
人工提出的新提案属于 main，并要求绑定的 main 日常 Session。

### authority 7

外发提案材料、审批化来源和发现材料使用 `research_egress_read_refs`，
对应确切 read 记录与正文 SHA，保存
`authority_kind="approved_research_egress_v1"` 和原 Artifact/Memory 上下文。
相关产物采用 **`authority_version=7`**；原版本 2～6 的合同仍分别按自己的凭据核验。

撤销原资料或记忆授权后，后续读取及派生产物继续检查这些原始依据；
人工批准不把私有来源降级为无依赖的公开数据。旧 reader 对新 authority 的兼容边界
以本轮原始真实资料、冻结c3源码和零网络冷进程的独立收据为准。

## 5. 状态与一次发送

| 状态 | 含义 |
|---|---|
| `proposed` | 原提案已保存，尚未批准 |
| `approved` | 用户已核对确切 revision/digest，尚未进入发送阶段 |
| `rejected` / `revoked` | 用户在发送前拒绝或撤销，原提案不再可执行 |
| `started` | 一次性发送准入已持久化，固定 nonce 已消耗 |
| `completed` | 原网络结果与来源/发现收据已持久化 |
| 派生 `stale` | 研究、来源、接收方配置或 actor version 已变化 |
| 派生 `expired` | 提案在发送前到期 |
| 派生 `unavailable` | 原身份或实际来源权限失效，返回 opaque 内容 |

`started` 不等于外部服务已经成功返回，`completed` 也不等于来源或研究语义已经独立核实。

发送准入在一个原 Journal 事务内完成：

1. 核对批准、有效期、当前基线和实际 actor version；
2. 固定 lease holder/fence 并记录一次性的 `sent` proof；
3. 将网络 intent 从 `reserved` 改为 `started`；
4. 将提案从 `approved` 改为 `started`。

准入后、传输前再核对依赖。即使这次重核发现来源已经撤销，
消耗过的 nonce 也不恢复成可重发批准。用户在 `sent` 提交之后撤销或取消，
不会被描述成此前请求从未进入发送阶段。

已捕获结果保存为原 intent 的 `captured` stage；重启后的继续处理复用该结果，
不再次查询或抓取。完成收据保存原 source/discovery 及确切版本引用。
`completed/rejected/revoked` 不退回可发送状态，原完成收据保持不变。

## 6. 原请求、UNKNOWN 与显式继续保存

GET 和重挂载只查询原请求，不自动提交 POST。

- 原网络 intent 仅为 `started`，没有可信 captured stage：请求状态为 `unknown`，不返回重发参数。
- 原 captured stage 存在、落库未完成：请求为 `pending`，返回当前 scoped view。
- 完成：返回原 `completed` 收据与对应 view，不改成新的来源身份。

对于原 `egress_execute` 请求，只有 captured stage 存在且 view 仍可访问时，
GET 才提供显式继续保存合同：

```json
{
  "status": "pending",
  "request_id": "original-execute-request",
  "resume": {
    "operation": "egress_execute",
    "arguments": {
      "id": "rx_0123456789abcdef0123456789abcdef",
      "request_id": "original-execute-request",
      "expected_revision": 2,
      "expected_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }
}
```

以上为合同示例，不是实际执行收据。`resume.arguments` 只含四个原始操作字段，
没有新的 query、url、ctx 或 request ID。提案 head 可能已经是 revision 3；
继续保存仍使用原 outer intent 的 revision 2，避免改变原请求 fingerprint。
`proposal.execution.request_id` 是内部 `egress-<rxID>`，与 outer UI request ID 不混用。

执行视图另返回 `execution.phase=started|captured|completed` 和 `saved_stage`。
UI 需要用户明确点击“继续保存原结果”；它只提交服务返回的原 resume 操作。
来源失效的 opaque view、未知请求和无 captured 结果均不提供该入口。

## 7. 入口

共享 Tool 为 `personal_research`，动作使用平铺参数。人工命令：

```text
/research egress-list --limit 20
/research egress-get <原外发ID>
/research egress-request <原request_id>
/research egress-propose-json '<完整提案JSON>'
/research egress-decide-json '<id/request_id/expected_revision/expected_digest/decision>'
/research egress-execute-json '<id/request_id/expected_revision/expected_digest>'
```

`*-json` 对象省略 `operation`，因为子命令已指定操作。decision 为
`approve/reject/revoke`，只由已验证的人工入口执行。

HTTP 使用现有认证和 no-store 合同：

```text
GET  /api/v1/personal/research/egress
GET  /api/v1/personal/research/egress/{id}
GET  /api/v1/personal/research/egress/requests/{request_id}
POST /api/v1/personal/research/operations/egress_propose
POST /api/v1/personal/research/operations/egress_decide
POST /api/v1/personal/research/operations/egress_execute
```

Web2 研究工作台复用这些实际 API：展示 actor、原研究、完整规范外发内容、
接收方、有效期和当前状态；批准和执行分开。私有正文不写入 localStorage 恢复槽，
原请求查询与响应 fencing 不把旧身份结果填回新界面。

## 8. 本轮实际证据

事务目录：`.naumi/transactions/pa07-egress-20261106/`。
实现前冻结 HEAD `c3c6187a7f8cea65bb1d6f1151b96a4201772eec` 的3203份tracked原文件。

### 集中验证及原失败保留

| 收据 | 实际结果 | exit |
|---|---|---:|
| `baseline-core.json` | 冻结c3：497 passed, 2 skipped | 0 |
| `backend-final-02.json` | 588 passed, 2 skipped in 645.15s；网络拦截0、强制终止false | 0 |
| `frontend-baseline-strict-01.json` | 严格冻结c3：575/575 | 0 |
| `frontend-all-final-02.json` | 38文件，606/606，含31项egress测试 | 0 |
| `frontend-egress-tsc-final-03.json` | TypeScript无输出通过 | 0 |
| `frontend-egress-build-final-03.json` | 4587 modules，built in 1.25s | 0 |
| `lint-final-03.json` | `All checks passed!` | 0 |
| `imports-final-01.json` | `IMPORTS=PASS shared_service,egress,transport,tool,api` | 0 |
| `identity-contract-final-01.json` | 24项范围、名称及身份合同：10 passed | 0 |

首轮后端4项失败与首轮完整前端598/600均保留。记忆目录读取清除mutation错误的
真实竞态已最小修复；增强原用例在冻结hook上确定性失败。Followups只修复测试取得
detached节点的时序，不改Today/Followups产品逻辑。严格原全套基线575/575通过，
不把它描述为复现了相同两项失败。因果证据见`FRONTEND_REGRESSION_REPAIR.md`。
三条相关路径独立提交，其余为外发模块提交。

### 原始真实模型、人工操作与公网

唯一新模型调用使用配置的`kimi-for-coding`，finish=`tool_calls`，4173输入+331输出
=4504 tokens；响应ID摘要为
`ffd9061a71001a8290b78e77d4f44ae3b6bcae33a13bcc8ce25106f4cea584c4`。
`cost_usd=0.017484`仅为rate-card estimate，billing_status=unsupported。
没有重放原付费身份。`real-propose-01.json`记录：

```text
EGRESS_REAL_PROPOSE=PASS model_calls=1 proposals=3 capture_calls=0 awaiting_human=True
```

原Session=`0add61d16dfa`，主提案=`rx_c01bc1660c135ddb15486d13bb3d84a4`。
主提案由真实Tool/ArtifactToolProxy创建；拒绝、撤销提案是明确的验收Tool输入，
不假称模型自主生成。验收使用本地明确canary资料，不代表真实私人账户数据验收。

生产UI完整展示原URL、接收方和原依据。人工批准后capture仍为0；另行明确执行后
真实生产capture只调用1次，HTTP200，正文219字节，max_redirects=0。
来源=`rs_e73ca4647380fccc4f3e213f0fa2e1e1` revision1，正文SHA256=
`5208593d737e1244c8d2e2ec62cbc0a2017511095e3c8656727313b6b72d218c`。
原trace含连接元数据，只保存私有正文，不在文档输出客户端IP。

```text
EGRESS_REAL_UI=PASS model_calls=0 capture_calls=1 original_states=completed,rejected,revoked
```

原生产API/Engine已关闭，端口已释放。实际公网证据是capture路径；
DuckDuckGo/Brave parser、固定单商及错误路径属于本地测试证据，未伪称两家实时搜索已跑过。

### 浏览器、恢复及权限证据

实际共享浏览器完成批准/执行/拒绝/撤销。后续选择器失败和目标超时保留原记录，
没有重做已完成写入；最终installed Chromium仅只读核对生产bundle与真实API。

```text
EGRESS_BROWSER_READBACK=PASS states=completed,rejected,revoked protocol=True writes=0 capture_count=1 mobile_overflow=False
```

桌面1440×1000、手机390×844；`egress-desktop-final.png`与`egress-mobile-final.png`
均已打开检查。原资料库打开exact actor/main、artifact/version1，读到`visit_scheme=https`。
最终只读阶段console/page错误0、研究写入0，capture仍为1。早期未填token的401留存，
不描述整个历史从未出错。所有临时浏览器已关闭。

captured阶段显式继续保存、UNKNOWN无resume、模型不能decide、撤权、过期、并发、
不回退服务及无自动POST等合同由当前后端/组件集中测试覆盖，不伪称每项都经人工公网复跑。
owner列表当前返回完整提案envelope；UI仅缓存摘要并GET详情，不宣称服务端列表省略私有字段。

### 独立冷读、旧reader及交付

`real-cold-read-01.json`核对原完成/拒绝/撤销身份、原来源v1与正文哈希，禁止模型/网络；
`rollback-data-boundary-baseline-03.json`在冻结c3进程上读取原真实authority7产物，
核对新产物拒绝、原普通资料可读以及逻辑DB/稳定文件不变。
两次验收脚本失败保留：第一次为内部ref比公开ref多`reader_id`导致投影断言不匹配；
第二次为额外凭据tripwire将配置读取误判成必须为零。最终第三个新只读进程使用
本地None凭据解析代理，记录2次解析请求，原生凭据后端访问0，模型/网络0；
原authority7拒绝及普通资料可读的断言没有放宽。
这些收据的实际命令、字面结果、退出码与源文件哈希汇入`VERIFICATION.txt`，
不将其与provider、浏览器写入或部署验收混合。

四份交付产物是`MODIFIED_FILE.py`、`DIFF_FILE.patch`、`VERIFICATION.txt`、
可执行`ROLLBACK.sh`。仅副本回滚验证全部3203原文件、逐个变更文件的后续编辑零写入保护、
原工作区拒绝、二次no-op及恢复后原路由/497项基线行为；实际结果和选择性提交SHA
以最终`VERIFICATION.txt`及`git-commit.json`为准，工作源码保留修改。
本轮没有安装发布、重启用户常用应用或执行真实Google账号读写。

## 9. 自审与明确保留的后续工作

1. **原 Session 的 recheck 上下文**：后续已接入明确的上下文checkpoint，详见
   [对话依据更新](PA-07-context.md)。它保留原Session refs和消息、原字节及活授权，
   显式历史镜像与当前来源checkpoint使原会话可以继续；来源再次变化仍要求重新确认。
   本文外发审批的原验收不替代后续独立集中验证、真实模型/浏览器、冷读和回滚证据。
2. **后台 Item 集成**：`ItemExecutionContext.authorize_tool()` 当前仍只允许既有代码、文件
   与部分 Artifact 操作。Agent 日常对话的外发审批不等于后台事项已经获准执行研究。
   其 policy、独立执行、恢复和送达还需对应实现与真实验收。
3. **独立语义复核**：`semantic_verification=not_performed`、
   `independently_verified=False` 保持不变。确切引用与数值复算不替代引文蕴涵、反证或
   推荐质量的独立核实。
4. **平台与完整产品**：真实 Google 账户、安装/部署、多设备、语音和电脑控制仍按各自
   模块验收；本切片不关闭这些目标，也不缩减 24 项视频范围。
