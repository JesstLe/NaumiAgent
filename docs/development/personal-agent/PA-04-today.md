# PA-04：今日事项、截止与优先级

日期：2026-11-05  
基线：`26b9fa8c74a4c935550680d7fdb3b137cb997eee`  
状态：代码、共享入口、本地 HTTP/冷重开和真实浏览器纵向闭环已验证；不替代完整 PA-04 或真实日历账户验收。

## 模块边界

- 每个已有 Agent 按日期和 IANA 时区维护独立日计划，不预置岗位、创建 Agent 或改名。
- 新增 owner-bound Today journal，包含 metadata/day/change/read。Item schema v1、定义 SHA、执行意图和 once 身份不变。
- 执行仍为原 Item → Job → Run → report/inbox；Today 没有第二套 worker，也不复制报告答案。
- 日计划当前只汇总当前工作区的已授权事项、周期和交付；跨工作区全局聚合仍待独立实现。

## 真实数据模型与一致性

metadata 保存确切 Item 引用、截止、优先级、归档与人工勾选。day 保存有序 placements 及 include/exclude；
change/read append-only。每项写入使用稳定 request ID，CAS、业务变化和原回执在同一事务提交。
重复请求返回原身份，不重复执行 callback；断线和 503 沿原 request 查询，不自动新建或重发。
跨日移动同时 CAS 源、目标计划，change 在提交前保存双方确切修订。外部实例 pin 防止删除库和内部 witness 后静默重建。

模型修改保存原会话 artifact/memory provenance；后续换对话仍检查原模型和当前读取者的来源授权。
回执绑定原 metadata/day revision 和 digest，不用新的干净修订复活旧来源。受理之后撤权保留 opaque accepted 回执，隐藏正文投影。
用户完整重设 metadata 或完整重排可形成明确的人类新输入；勾选、移出和保留旧字段不自动去除原 provenance。

## 来源读取与模型边界

- Item 读取同时校验当前范围、原历史修订、原 assignee、原资料和 intent 引用。
- 生命周期 close 不误报语义变化；once/hash/assignee 变化必须显式 rebase，清除旧人工勾选。
- 已交付按真实 delivered_at 归日，不用意图 issued_at。旧 once 的报告也可 keyset 分页。
- 模型 get/metadata 从原参数重建，不信任调用者拼装的 public result。模型正文移除易变 observed_at。
- 返回正文和引用在 await 前 detach；真实 material bytes、SHA、read journal 与 SessionStore 绑定一致。
- artifact evidence 只保存严格 read pointer，来源清单在 append-only read journal，避免 64 KB evidence 上限。
- 现有 artifact 和 runtime fence 在下一模型/工具执行前重查来源；受信任委派执行仍需活跃的原 execution authorizer。
- excluded tombstone 不带正文，不阻断其余干净资料读取；失效 included 条目只返回 opaque 身份，不把标题或派生字段送回模型。

## 时间与界面

真实 TZif 决定民用日边界，覆盖缺失时刻、重复 fold、23/25/47 小时日、跳日和非整点 DST。
date-only 截止为该民用日排他末端，instant 必须显式处理歧义，不用浏览器本地时区替代 IANA 解析。
截止保存原 UTC 与 timezone SHA；数据变化不自动重写，编辑器展示原 UTC 并要求用户确认后重新解析。

浅色 Today 提供日期/时区/Agent、真实事项选择、截止/优先级、拖放与键盘完整排序、人工勾选、移出、跨日移动、显式 rebase 和历史。
三类来源分别显示错误与 partial，不用空列表冒充完整无结果。CAS 冲突保留草稿，namespace/Abort 与 dirty guard 防止旧工作区内容混入。
打开交付沿原 occurrence 的 ResultDetail；只有原 SHA 绑定的人工已读接口能记已读，Today 浏览不改原状态。

## 共享入口

`personal_today` Tool、`/today` CLI/TUI、14 条强 header/no-store REST 路由与 Web2 Today 共用 service。
模型没有 check/move/planner/admin/trusted 字段；五类计划写操作走原工具确认，读取不制造确认打扰。
手动命令只向模型历史写 opaque receipt，不回灌未 pin 的原参数和正文；用户界面显示真实服务结果。

## 验证与自审

事务：`.naumi/transactions/pa04-today-20261105/`。原件和完整 3074 tracked 基线副本在修改前冻结。
原 capability probe 为 tool=0/HTTP404，新增模块的 mounted probe 为 tool=1/HTTP200，均为本地 Engine/ASGI，provider 为零。
中间 218 项测试通过时源码仍在修订，不作为最终冻结证据。Havana 2026 的旧测试错误使用未来 DST 假设，
修为真实已发生的 2020 午夜 fold，并用独立 ZoneInfo offset/roundtrip 核验，未修改 resolver 来迎合预期。
最终命令、完整 literal output、退出状态、源码回滚及后改零写入保护以本事务 VERIFICATION.txt 为准。

本模块受控 StreamChunk 用例真实创建 Job/Run/report/inbox，但不是 provider 验收。
Google Calendar 是用户已选首个平台；账户 OAuth、读取、确切人工审批、外部写入及原事件读回的 live 状态独立记录于 PA-03。
通知合并/勿扰、跨工作区、完整持续助理、发布安装和整体24项验收没有因本模块自动完成。

## 最终集中证据与恢复结果

- 冻结后端组：`314 passed in 318.79s`，exit 0。包括全部 Today 五个文件、API middleware 和原连接集成；源码与测试的最终 SHA 在事务内固定。
- 全前端此前完整组 `33 files / 398 tests` 通过；最后的空安排恢复交互修订后，Today 完整组件组 `20 passed`，生产 `tsc -b + vite build` exit 0。两组有重叠，不相加作为独立覆盖数。
- 真实 loopback HTTP 完成 54 次请求及两个独立服务进程的冷重开：原 Day/metadata/Items/history/requests/实例逐项一致，零模型、零新增 Run、零非 loopback、零已有配置或用户数据读取尝试。正常成功、401、CAS409 与原回执分开核对。
- 旧 `place-first` 响应 timeout 不被重发：冷进程仅 GET 原请求、确切日计划和字段、原事项与历史，证明唯一受理身份与记录未变化；`BUSINESS_POSTS=0`，自身服务正常退出 0。
- HTTP 实际发现 stale 条目仍带旧标题，已在服务层清空标题/assignee/report/due/status 并保留 opaque 旧 ref；真实完整闭环和最终 314 项回归均在修订后通过。先前失败保留。
- 独立浏览器使用实际生产 bundle + 真实隔离 Engine/HTTP，完成真实选择器加入、完整排序保存、人工勾选、跨日移动（确切源/目标回执）、移出和日期导航五项写操作。原 Item JSON 前后相同、pending 为零、Run/provider 为零；不是 mock UI。
- 实际浅色渲染保存为事务 `today-browser.png`，已打开检查。浏览器标签关闭，确切 ready PID 的服务退出码通过 Windows 进程句柄核为 0，保留 graceful marker。
- 旧宽批次因自审修订而主动停止，另一次后端执行因运行态丢失没有终态记录，两者都没有算作通过。首次前端 launcher 缺失和 wrapper 编码错误也独立保留，不改写原失败。

剩余范围仍按原合同推进：跨工作区、通知合并/勿扰、真实 provider 和真实 Google 账户、安装部署。源码回滚只操作独立副本，不撤销数据库历史或外部账户动作；结果以本事务 `VERIFICATION.txt` 与 Git 回执为准。
