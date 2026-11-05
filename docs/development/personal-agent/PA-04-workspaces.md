# PA-04：跨工作区事项与原结果汇总

日期：2026-11-05
基线：`1b22de6dcc437b1c752c9507659e0ac3ac27aa30`
状态：本机在线源的纵向实现已落盘，集中验收和真实多进程浏览器验收独立记录。PA-04 与完整 Personal Agent 保持 `partial`。

## 产品闭环

1. 用户在汇总端选择已有 Agent，生成公开接收卡。
2. 用户在原工作区明确选择源 Agent、接收卡、读取事项/结果的范围和有效期，签发读取授权。
3. 用户在汇总端填写已在线的 loopback API 地址、本地名称和确切凭据，核验原身份后连接。
4. 汇总分来源读取真实标题、原执行时刻和时区、事项状态、原执行状态；用户可打开原已送达回答和报告正文。
5. 原工作区撤销/授权到期、汇总端断开、Agent 版本变化、实例或文件身份变化、源离线时停止读取；健康来源继续显示。

“接收卡”是公开绑定，不是密码。读取凭据只在本次人工传递和显示期间出现在内存；SQLite 只保留独立原生系统凭据密钥加密的 envelope。URL、模型参数、项目设置与模型历史不包含明文凭据。

初始仍只有主 Agent；连接不创建持久 Agent、团队或执行子 Agent。源端和接收端的 `main` 是不同主体，不按同 owner、同名称或最近项目自动授予访问权。

## 原执行权不变

汇总服务不构造 Engine，不启动 daemon/worker，不调用 scheduler、reconcile、create/restart/cancel、人工 read 或外部审批。源未在线时显示不可读，不为了显示事项隐式激活工作区。Web2 保持当前 runtime/session/token；只读原结果在汇总面板打开，不复用会启动 daemon 的 `activateWorkspace()`。

`PersonalItem → AgentJob → Run → artifact → inbox delivery → human-read` 仍是原工作区的权威链路。事项 `closed`、执行 `completed`、结果已送达、原报告人工已读分别保存。打开汇总和报告不新增原已读回执。

当前汇总对象是已登记的远端本机工作区；当前工作区原“事项/今日”入口保持不变。只读汇总不跨工作区改计划、勾选完成、审批或重新执行。显示的是原一次性事项执行时刻，不把它冒称 Today 的人工截止时间。

## 身份、授权与凭据

- `workspace` OwnerJournal 保存 grant、connection、source_read、read；复用 owner、CAS、不可变 history/request 和租约证明。grant/connection 不重绑定、不重新激活，撤销后需新授权。
- 外部 `.personal-workspaces-<workspace_key>-v1` pin 独立保留目录实例。删除/替换已登记资料库、原 witness、路径别名、symlink/hardlink/reparse 都不能静默创建空目录。
- 源绑定 owner、原 workspace/session DB 路由、登记实例和原 ItemStore 实例；私有记录同时固定 catalog/session 的 native device/file ID。目标固定完整接收卡及 Agent 配置 digest。
- 登记实例不是旧 catalog/session 的数据库 UUID；路径 hash 不是账户认证。原数据库当前没有内部 instance API，本模块没有伪造这种证明。正常进程重启可继续；同路径替文件后旧授权停止，需人工重新登记授权。
- 原生凭据服务独立为 `NaumiAgent.PersonalWorkspaces.v1`，只允许 `wg_/wc_` 槽位，AEAD AAD 绑定域、owner、slot、purpose。没有明文文件回退，也不复用 Google 的密钥槽。
- 只有人工 Web/CLI/TUI 可签发、连接、撤销、断开。模型只读 schema 为 context/list/overview/result，调用者取自当前真实 Agent，不接受身份、owner、地址、通道或权限覆盖字段。
- 凭据与创建请求采用稳定 request ID、CAS、持久租约和 opaque 回执。提交后的网络、投影或 lease cleanup 失败不变成“未受理”；UNKNOWN 只核对原 request，不创建新连接。单次凭据丢失后由用户撤销再签发。
- 授权最多 30 天。观测到期后持久撤销；时钟回退和进程重开不使该授权复活。时间早于签发时刻时停止读取。

## 原源校验与互证传输

只支持显式输入的 `http://127.0.0.1:<port>` 或 `http://[::1]:<port>`。固定一个 federation 路径；不做端口扫描、DNS、环境代理、redirect、任意 URL 转发或目录实例重绑定。

请求使用 `NaumiWorkspace nonce:unixSeconds:HMAC`，绑定独立 request 域、路径、完整 JSON 和跨源 trace；响应使用独立 response 域和本请求 nonce 的 HMAC。明文 shared secret 从不发送给远端 API。端口被其他服务占用时，该服务得到的是本次签名，不是凭据；错误 proof 的内容不进入汇总。

nonce 以原 OwnerJournal 的 once/sent 租约持久防重放。时间窗口为 60 秒，trace 最多 8 个唯一来源标识，跨源祖先循环停止。客户端并发 4、10 秒总边界/5 秒 transport timeout；源端并发 4、15 秒包含等待的总 deadline。请求不超过 256 KiB，响应不超过 2 MiB；强制 identity encoding，在流读取/自动解压前拒绝压缩响应。单份报告正文不超过 256 KiB，单结果材料总量不超过 512 KiB；超出时如实不可读，不默默截断成完整报告。

源端重用 `source_page/_scope → Today._authorize_item → 原 executor.view/items.result → Artifact.read/authorize_refs`，不以人工 `None` 作为 grant source actor。正文只限原确切修订和原材料授权；原输入撤权不能靠换新分享、改事项标题或读取另一份报告绕过。

源页沿原 keyset 游标分页，失权条目不返回标题/名字/正文；空过滤页保留原 continuation。`source.partial` 区分分页或部分不可读，来源失败不当作零事项。列表不返回回答或报告正文。

## 模型真实材料与旧版本栅栏

源端保存不可变 source_read，绑定返回正文 SHA、原 ItemRef、原结果/送达身份及原 artifact refs；hub 保存不可变 read，绑定 connection revision、源身份、当前 Agent digest 和原 source_read。两端 journal 都不保存清晰正文。

模型获取的结果重新查询真实来源，实际返回 body 等于发布的 UTF-8 material bytes；绑定当前持久 session，并在绑定前后及下一次模型发送前复验。失权/离线/断开时旧 material、转交材料和衍生产物同样停止读取。缺失 workspace service 时沿完整祖先 fail closed，不降级普通资料。

带 workspace_read_refs 的原材料采用 `authority_version=4`，旧 broker 明确拒读，避免代码回滚后把动态来源当普通文本。对该材料的 revision 会保留前版确切 source_ref；删除 evidence、改标题或修订内容不能清除旧依赖。普通旧材料、connection v2 与 routine v3 保持原语义。

## 共享入口与 UX

- `personal_workspaces` Tool、`/workspaces` CLI/TUI 与 REST 使用同一严格 dispatcher。
- GET `/api/v1/personal/workspaces/context`；POST `/operations` 为人工操作，独立 POST `/federation` 只接受相互证明。
- 普通认证旁路只适用于精确 federation POST；其他方法/路径保持原认证。所有工作区响应，包括 401/429/404/405/307，带 no-store。
- Web gateway 的源凭据仅返回给人工流；session、run step 和模型历史只保存 opaque request/run/operation/status，不保存 ticket/secret 或非法 token。
- 浅色“全部事项”、来源管理、接收卡、签发/连接表单与只读原结果。每来源 namespace、revision、route 和序列栅栏防止断开后晚回填旧正文；独立部分错误不清其他健康来源。
- 所有本次凭据字段仅内存保留，离开/断线/namespace 变化清除；不存 localStorage。localStorage 仅为 UNKNOWN 保存 opaque request 身份。

## 验证与交付记录

本模块事务：`.naumi/transactions/pa04-workspace-federation-20261105/`。

- 3119 tracked 原件及 SHA 在首次写产品前冻结；baseline probe 为 `WORKSPACE_FEDERATION=ABSENT`/exit 1。
- 单元、真实存储、接口、前端、独立只读审查、真实 loopback/冷进程/浏览器、回滚与最终源码哈希分别保留原日志。中间失败不改写为成功。
- 三个归属子模块和本模块最终整合结果以事务 `VERIFICATION.txt` 为准，不相加重叠测试数。
- 真实 HTTP/浏览器复用原通知04的两份已送达报告，保持原路径身份；不付费重放旧 Run、不改原结果已读。新 hub/source2 是独立明确验收夹具。
- 真实 Google OAuth/账户写回读回、OS/mobile push、跨设备/cloud、安装部署与容量验收仍分别待验收，不由本机只读 federation 证据代替。完整 24 项能力目标保持 active。

## 本模块最终本机验收（2026-11-05）

- 最终集中后端：`544 passed, 3 skipped in 675.30s`，exit 0。包含工作区服务/源权威/传输/接口/store/vault/persistence、原 ArtifactStore/session race、Today/通知授权及 Web commands；不称全仓 pytest。3 个 skip 是当前文件系统禁止替换已打开 handle、无非特权文件 symlink、无目录 alias。
- 基线兼容：原 1b22de6d 完整源码副本的 ArtifactStore/Today 相关 `95 passed, 2 skipped`；基线 federation probe 为 ABSENT/exit1，修改后 PRESENT/exit0。
- 前端完整串行：35 files / 482 tests passed；最终工作区 31/31。最后 TypeScript + production build exit0；原有 large-chunk 提示不冒充构建失败。响应式修正后真实浏览器 filter >=173px，390px 面板 scrollWidth=390，无横向溢出。
- 真实三进程 HTTP 和生产组件：公开接收卡→两源 UI 签发→hub UI 连接→3 原事项→原回答/报告→撤源清正文保健康源→hub 断开通过；全部 browser HTTP200、errors=[]。
- hub/source 冷进程读回原 connection/session/grant；同端口 impostor 错误 proof 被拒为 source_stale，entries 为空，健康源继续可用；恢复原 source 后同 grant 可读。
- 真实 model_result 物化 2470 bytes，body/artifact SHA `90713f2ca9e6691946f225aecf69d1f5c5a4bce75dabc0a47cc6daef87dfcfc0` 完全一致；源撤权使原/派生 material 和下一发送均被阻断。此项没有外部 provider 调用，不冒称新的真实模型生成。
- 复用原通知04的 15 库/99 表全 schema + all rows SHA 在各阶段一致（摘要 `f27f3ee5d8d8a501c4e605d7903d806519791ec8cc58898417673ba1f80b11ee`）；原 Job/Run/receipt/delivery/人工 read 保持原身份，新增执行派发、worker、provider 均为0。
- 原 catalog 49152bytes 的物理 SHA 从 `f786cf519d6d917350f965e9be81abaaa985ba9d69ec13e9c65bfd8589a08528` 变为 `327bdda9a941c2df82fb3211e0520dde9e04ec6bd8dbbc26eeafc44a8a5379f5`；业务 schema/行未变，没有人为恢复原库，也不推断具体物理原因。
- 35 次实际相互证明发送均 contains_raw_secret=false；8 个本次验收原生槽位删除并独立读回 absent；所有自建 runtime/impostor clean exit0，59007/59008/59009 无 listener，harness Python 进程为0。
- 独立只读审查的6项边界已修：旧reader降级、领域异常码透传、postcommit投影、lease cleanup、源整体deadline、响应解压前容量保护。原复现及首次测试/夹具失败日志均保留，不改写成从未失败。

代码副本回滚的逐文件 guard、全组零写保护、原件恢复、原行为重测及最终独立 commit 以事务 `VERIFICATION.txt` / `rollback-rehearsal.json` / `git-commit-receipt.json` 为准。所有业务库和 OS 系统服务均不参加源码回滚。
