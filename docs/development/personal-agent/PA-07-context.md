# PA-07：来源复查后的对话依据更新

日期：2026-11-06

状态：本切片实现、集中回归、实际模型/HTTP/生产 UI 与冷读已通过；副本回滚与
选择性 Git 交付以最终事务收据为准。完整 PA-07 和 24 项产品目标保持 partial。

## 问题与决策

原模型读取结果是确切 Artifact，Session 永久保存其引用。来源复查改变 source head
后，旧 live read 按原 latest digest/expiry 合同停止后续模型发送。重新读取新研究或
只追加新 refs 都不会移除旧 pin；静默删除旧引用或放宽全部 freshness 也会丢失依据。

本模块采用明确的上下文 checkpoint，而不是修改旧对话：

1. 人工针对实际 Session/actor GET 预览全部原引用及来源变化。
2. 用户核对后提交原 context revision 和 plan digest。
3. 服务保存不可变 `context_update`，为相关原根建立逐字节相同的历史镜像。
4. 同一 Journal 事务 CAS 提交 `context_result` 与 `context_head`。
5. Session raw refs 保持 append-only；有效引用解析将原根映射到镜像，未覆盖的新输入原样保留。
6. 真正模型发送前，在待发消息副本中加入明确历史说明和当前来源 checkpoint，旧消息不改写。

这不把旧结论更新为新事实，也不等于独立语义核实。

## 组件和合同

| 路径 | 职责 |
|---|---|
| `personal/research_context.py` | 严格操作、预览、不可变上下文凭据、镜像、原请求、有效引用及待发说明 |
| `personal/research_store.py` | 同一 OwnerJournal 新增 context_update/result/head；update/result append-only |
| `personal/research_authority.py` | 仅对凭据列出的 exact old read IDs 使用历史 freshness 语义 |
| `personal/artifact_store.py` | 严格 context proof 和 authority_version=8 |
| `personal/artifacts.py` | 完整领域授权外层 scope、有效 Session refs；原持久引用不删不改 |
| `orchestrator/engine.py` | 待发副本注入历史说明，之后仍走原最终授权 fence |
| `research_tools.py` / `research_commands.py` / `api/routes/research.py` | 共享底层操作，人工与模型身份分离 |
| `ResearchContext.tsx` / `useResearchContext.ts` | 对话依据预览、两步明确确认、原请求恢复和身份 fencing |

操作：

```text
context_preview(session_id, actor_id)
context_rollforward(session_id, actor_id, request_id, expected_context_revision, expected_plan_digest)
context_request(request_id)
```

HTTP：

```text
GET  /api/v1/personal/research/session-context/{session_id}?actor_id={actual_actor}
POST /api/v1/personal/research/operations/context_rollforward
GET  /api/v1/personal/research/context-requests/{request_id}
```

CLI/TUI：

```text
/research context-preview <原session_id> <actor_id>
/research context-rollforward-json '<完整JSON>'
/research context-request <原request_id>
```

模型 schema 只有 preview/request，禁止 rollforward。已 stale 会话仍在普通工具入口前被
原授权 fence 拦住；首次修复通过真实人工控制入口执行，不给模型 `skip_authority` 开关。

## 原权限、原字节与原身份

- 镜像正文与原 Artifact 的 SHA256 一致；原 root 本身作为确切 source_ref 保留，因此原 direct
  grant 与递归 grant 仍由 ArtifactStore 检查，不把别人的资料复制成无依赖的自有资料。
- Page JSON 内嵌 section source_refs 继续保留；镜像也核对这份精确依赖闭包。
- ContextVar 只豁免不可变凭据明确列出的 read ID/digest 的 freshness，身份、正文、
  notebook 历史摘要、Artifact/Memory 及 Calendar/Routine/Today/Notification/Workspace/
  Followup 的现有权限仍运行。scope 包住完整 `_checked`，在退出或异常时恢复。
- 原 stale Artifact 单独读取仍按旧合同拒绝；镜像及其派生数据才拥有明确历史语义。
- 重新复查来源、时效跨界或 actor version 变化会使旧 checkpoint 失效；用户可以明确重新预览
  与确认，旧镜像不会因此获得全局有效性。
- 当前镜像属于原 actor 的上下文；给另一 Agent 一个 Artifact grant 不替代原非 publication
  read 的身份合同。跨 Agent 研究交接使用明确可分享的历史报告，接收方上下文合同仍需单独扩展。

## 并发、恢复与发送

- 原 context revision CAS、plan digest、原 raw refs 和当前 source checkpoint 均核对。
  最终提交事务内再次校验 source digest/expiry 和 immutable read 记录。
- 同 request ID 不同输入冲突；相同完成请求返回原收据，不新增镜像。
- 镜像已保存但 result/head 未提交时，在其他调用中仍不可读；旧会话不自动恢复。
  GET 仅返回原 pending/unknown 状态，不自动 POST 或另造 request ID。
- raw refs 后续追加不被替换或丢弃；有效 refs 仅自身去重。新无关失效依据仍会阻断发送。
- 同 Engine 活跃 turn 会拒绝更新；其他 Engine 的持久 nonterminal Run 也要求先核对原执行。
  nonterminal 数据行不被当作已确认进程存活，也不自动篡改成结束；原运行恢复由既有 runtime 负责。
- 持久 Session.messages 和 Engine 原消息保持原值。历史说明只插入最终待发副本，
  每次发送都从原 checkpoint 重建，压缩不会删除权限凭据。

## UI 与原请求

“对话依据”固定在研究工作台顶栏，目标来自 Web2 实际 Session 和已确认 actor；
pending/error/read-only 不使用 main fallback。预览只有 GET；“更新依据”后另行“确认更新”才 POST。

localStorage 只保存 namespace/workspace/actor/session/request 的 opaque 身份，
不保存正文、标题、原 refs、完整预览或 plan digest。重挂载仅查询原请求；
断连、API identity、Session、actor 或 writable 变化都会 fence 迟到结果。

## 本轮证据

事务目录：`.naumi/transactions/pa07-context-20261106/`。

修改前冻结 HEAD `12e33cfd51d21aba73a4676d2c9d2ad436058acd` 的 3213 个 tracked 文件，
原件及回滚副本逐个重新读取。原基线 498 passed / 2 skipped，exit 0。

首个 backend-01 超过预设 180 秒，被本轮 runner 终止；10 项通过、1 项 late-bind 合同失败。
原输出、失败及终止身份保留，不把它当成完整测试结果。late-bind 用例现显式核对“raw ref 已保存、
随后完整授权因旧依据失效而拒绝”，保留原计划失效、新计划恢复及不丢后加输入的全部断言。
后续集中批次使用更长真实执行时限；不重放付费或网络请求。

前端最终聚焦 90/90（新 Context33 + Research26 + Egress31）、TypeScript 和4590模块
生产构建通过。完整前端两次分别637/638、638/639，均保留同一原 Followups 失败。
冻结12e33的单项1通过/58跳过和整份59通过不能证明全套同项基线失败；相关七条原源码
字节未改，不把当前全套写为通过。一次事务 observer 未输出观测行，激活未证明，
不把该结果冒充因果修复证据。

### 既有 Followups 产品竞态与最终回归

后续只读追踪发现具体状态反例：父组件读取 URL 后把当前已经选择的同 ID 再传入，
旧 hook 无条件清空 selected/readback、推进 detailSequence；selectedId 未变化，
detail effect 不会因此重新读取。已核对的安排可能消失而 notice 保留。
新增确定性用例在冻结12e33 hook 下失败（1 failed /60 skipped，exit1），
独立证明该竞态；原 full606 基线仍保留为通过，不声称同次失败调度已追踪。

最小修复只让“外部 ID 不同于实际已选 ID”时才重置。新用例还检查不同 ID 与
namespace 边界，原 hidden-refresh UI 用例及全部原断言逐字未改。
两条修复路径单独提交，详见事务 `FOLLOWUP_SAME_ID_REPAIR.md`。

最终集中结果：

| 收据 | 实际结果 | exit |
|---|---|---:|
| `backend-final-02.json` | 652 passed, 2 skipped in 1228.76s；未强制终止、网络拦截0 | 0 |
| `frontend-context-followup-focused-final-03.json` | 5文件179/179 | 0 |
| `frontend-all-final-03.json` | 39文件641/641，81.64s | 0 |
| `frontend-tsc-final-03.json` | 无输出通过 | 0 |
| `frontend-build-final-03.json` | 4590 modules，built in 1.40s | 0 |
| `identity-contract-final-01.json` | 10 passed | 0 |
| `lint-final-03.json` / `imports-final-01.json` | Ruff及真实import通过 | 0 |

这是相关后端模块范围，不冒充整个仓库 pytest 全套。

### 新的实际公网、人工 UI 与模型继续

本轮新独立 `real-fixture-01`，没有修改或重放过去的付费 fixture。
实际 HTTPS 来源先采集v1（214字节），真实 Tool/Proxy 固定原 source-v1 返回和消息，
再实际复查一次得到同 source v2（219字节），两次均HTTP200。准备阶段模型调用0。
原 Session 授权确实抛出 source_stale；原历史 Tool 记录是明确验收输入，不假称旧模型生成。

- Session：`82c3f4012149`
- 来源：`rs_5d3f8424f3d2f89e1b5335bd97911ade` revision2
- v2正文SHA256：`7786dbd14fe30171dbbef2932a2c23fbeb2748bd0eea38a080cae997425cada1`
- 上下文：`rcu_0e30f98bc070796ff97523c18fc49c0c` revision1

实际共享浏览器打开原对话，经“对话依据→更新依据→确认更新”仅POST一次，HTTP200。
重载后GET读回current，未再次POST。原Session raw pins、消息和镜像字节均由原存储读回核对。
浏览器最初未设置隔离token的401保留；早期resize后两份截图正文缺失或裁切，也保留，
不把它们作为视觉通过证据。

在此原Session上，配置的`kimi-for-coding`实际调用两次。第一调用自主选择
`personal_research.source`确切来源revision2，经真实Proxy执行；第二调用基于原正文回答：

```text
source_revision=2
visit_scheme=https
旧上下文属于历史
```

7334 +3995 =11329 tokens；`0.037071 USD`仅rate-card estimate，
billing_status=unsupported，不当作账单。两份原响应ID摘要与usage存于model.json。
原完整trace保持私有，文档/终端不输出客户端IP。

独立冷进程模型/DNS/网络/capture尝试均0；原捕获累计数仍为2，原context/source/
Session身份和数据库逻辑不变。旧读取器使用冻结12e33，明确拒绝真实authority8镜像，
同时读回普通source-v2确切219字节；690个模块均来自原源码，7DB与21稳定文件不变，
模型/网络/原生凭据调用0。

最终独立installed Chromium只读读取新的生产bundle，实际HTTP index SHA与最终dist一致：
`241bc836147b79a186b30f8cc9212a65db2e97e67595a68966ff4d71238b98db`。
桌面1440×1000、手机390×844的`context-final-desktop.png`和`context-final-mobile.png`
均已打开检查，手机无横向溢出；只读阶段研究写入0、console/page错误0、新模型/抓取0。
服务端只读验收37条API记录，业务及元数据变化均0，服务/Engine/端口都已关闭。
两条已有Workbench Stable Population finalization projection失败日志保留，
不声称整个服务历史没有任何日志错误。

### 副本恢复与交付

本轮30路径（Followups修复2 + Context28）分别选择性提交。
`MODIFIED_FILE.py`、`DIFF_FILE.patch`、`VERIFICATION.txt`、可执行`ROLLBACK.sh`
保存核心副本、完整补丁、精确命令/输入/输出/退出状态及恢复入口。
副本恢复、全部3213原哈希、30项后续编辑零写入保护、恢复后498项原基线与原404行为，
以及实际Git SHA，以最终机械收据为准。工作源码保留修改。

## 自审与剩余项

- 当前切片已有上述独立真实闭环；完整副本回滚与Git交付仍以最终收据逐项收口。
- 这解决研究时效变化后的明确上下文连续性，不是独立语义复核、后台 Item 研究执行或真实
  Google 账户闭环。原24项范围、安装/发布、语音、电脑及多设备目标保持原验收要求。
