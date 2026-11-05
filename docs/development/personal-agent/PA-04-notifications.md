# PA-04：持久应用内通知与勿扰

2026-11-05。基线 `7f4d6d7a`，实现分支 `codex/personal-agents-pa00`。

## 本模块

应用内通知拥有独立的来源引用账本，不拥有新的执行器。初始仍只有主 Agent；用户创建的 Agent 保持原名称和身份。通知覆盖原事项结果、认证终态失败和持久日历草稿决策；临时内存权限请求仍走原权限条，不在重启后复活。

- `personal/notifications.py`：独立生命周期、限量扫描、合并、领取、呈现、通知已读及设置。
- `notification_authority.py`：原 Item/Job/Run/delivery、确切日历草稿、账户/分享修订和产物来源重新授权。
- `notification_store.py`：复用 OwnerJournal，启用 domain hashes、lease proofs、CAS 和不可变历史。SQLite、原 witness 和会话运行目录外部实例 pin 一起校验。
- `notification_models.py` / `notification_schedule.py`：严格字段和真实 TZif；跨午夜、DST fold/gap、半小时跳变、跳日、相接/重叠区间和连续全周勿扰。
- 共享 `personal_notifications` Tool、`/notifications` CLI/TUI、十个 REST 路由及 Web2 浅色通知抽屉。

## 来源与扫描

通知只保存稳定引用，不复制报告答案、错误正文、邮箱、私有路径或日历行动参数。每次列表、批次、领取、呈现、确认已读和模型发送均重查原来源。原来源失权或缺失时，通知保留 opaque 身份并清除标题、Agent 名称和跳转。

结果通知核对原历史 Item、原执行意图、认证 Job、Run 完成回执和确切 publication/delivery。`AgentJobStore.get_result_inbox_record_for_job()` 沿原 HMAC/session routing、投递绑定和 acknowledgement 验证逻辑精确查询，不依赖截断的前 100/1000 个收件箱结果。

失败来源是认证生命周期 receipt 的 `error/timeout/max_turns`，不按本地化错误字符串猜测；基础设施失败即使没有 personal-item manifest 也保持原事实。cancelled、unknown 不被改称失败或成功。

日历通知绑定原 action revision/digest、draft revision、账户 epoch、实例、Agent 版本、原 grant 和原有效期。重授权不复活旧 grant；修改或审批后旧引用变为 resolved，撤权清正文。通知按钮不批准或执行外部动作。

随机 occurrence ID 不作永久高水位。每个完整扫描轮归零，独立 pending watches 补扫晚完成；来源、watch、action、outbox 均有界分页。worker 在取得新 fence 后重新读取持久 scan，并 CAS captured revision，保留时钟高水位；旧执行者和过期租约停止写入。扫描不重放 Job/Run。

## 批次、勿扰与三类事实

1. 原 Job delivery 证明入箱。
2. `presentation` 仅证明客户端报告确切完整批次已实际可见。
3. `notice_read` 仅证明用户显式确认通知已读。

三者互不推出。GET、角标、打开详情、模型读取和 toast 均不写人工已读。通知已读永不调用原结果 read API；原 ResultDetail 保留自己的 SHA 绑定确认按钮。

批次按 Agent + 来源类型分组，成员封存后不可变，最大 100。普通结果按 debounce/最大等待合并；失败和决策可立即成批。ready outbox 与已领取历史分开；只读提示游标公平轮转，不受随机历史前缀永久阻塞。部分已读/失权的不可变批次不反复提供必然失败的自动领取，原通知仍可主动查看。

首次设置明确选择是否纳入历史。默认仅设置后新事实；后续修改不会偷偷移动原 cutover。关闭主动提示或勿扰/静音不阻断原结果入箱、通知物化或主动查看。逐 Agent 静音优先于 bypass；bypass 默认空。时钟回退、TZif 漂移暂停主动提示，并保留明确状态与列表。

claim 原子保留一次逻辑提示，绑定 client/batch/digest/完整 event refs。页面在发送前持久化 opaque 请求/阶段；响应未知只 GET 原请求，重载后不自动重弹。实际 DOM 全员可见才 present，未滚动看到的成员不虚构呈现。进入勿扰、隐藏窗口、来源失权或 namespace 切换时清除主动提示。

## 统一入口和模型

REST 使用 header-only 身份验证、no-store（含 middleware 早期 401/429）、256KB 限量、重复 query/JSON 与未知字段拒绝。设置和 claim/present/read 只能由外层注入 web/cli/tui 人工通道，模型没有这些操作。

owner-wide 请求账本的查询仍受 workspace 约束：从历史收据引用的 preferences / batch / event 身份校验本域，其他 workspace 返回 unknown，不暴露 foreign opaque identity；本域来源失效后的原已读收据仍可 accepted。该检查兼容已有收据，不补写历史 scope 或迁移 schema。模型请求查询始终不返回人工收据。

模型返回从原 query 重建，经不可变 `nx_` 读取凭据和实际 material bytes 绑定到当前持久会话，再复核当前 actor/version/session 和来源。后续发送及派生产物重新核原依赖；通知组件缺失的 ancestry fail-closed。手动命令正文/原始 JSON 不回灌模型历史，只保存 opaque 命令收据。

## 读取调度与计数语义

真实浏览器发现快速开关抽屉、筛选及切换工作区会重复启动 context 请求；客户端 abort 不等于服务端来源遍历立即结束。context 轮询现与列表读取分离，同一 query 采用单 in-flight；周期刷新不取消仍在读取的同 query。namespace/filter 变化仍清正文、abort 并按 sequence 隔离，context 失权在同一 tick 阻止晚回列表正文。

context 不返回来源标题或报告。未读数量原本就包含来源失效的 opaque 通知，现在从同一认证短事务内的 event / notice-read 计算，保留 actor、workspace、receipt digest、10,000 与 partial 边界，不用 raw SQL count，不截断已读收据，不跨请求缓存。未读计数标记为 `journal_snapshot`；并发已读可能在该快照之后提交，不将计数称为线性化的即时事实。需要处理的数量仍对高优先未读来源投影并在返回前再次授权，resolved / unavailable 不计入。context 成功不代表未读取的 outbox 或全部来源链健康；列表、详情、领取、呈现、通知已读和原结果入口仍各自完整校验。

独立 cProfile 定位了 Windows 路径 / 实例验证的重复开销：原两条来源的 context 调度 to-thread 166 次、list 317 次；profile 时间分别约 5.9 / 11.1 秒，包含 instrumentation，不能当作无 profiler 的 SLA。后续进一步合并来源 proof / descriptor-bound 验证须独立证明 owner、witness、hardlink、reparse、撤权和最终发送 fence，不能用全局授权缓存或提高超时冒充优化。

通知列表的后续独立优化已将普通计数与来源正文分离、本域 metadata 合入短批量快照，并保留初次／末次来源授权、最终 receipt 过滤和 raw keyset cursor。[列表读取记录](PA-04-notification-reads.md)分别记录同一真实数据的前后 HTTP／生产组件测量、集中回归及源码副本回滚；不是跨请求正文或授权缓存。

## 验收记录

事务目录：`.naumi/transactions/pa04-notifications-20261105/`。完整命令、literal output、exit status、源码 hash、原失败、集中回归、HTTP/冷进程、生产组件浏览器及副本回滚见该目录 `VERIFICATION.txt`。四产物为 `MODIFIED_FILE.py`、`DIFF_FILE.patch`、`VERIFICATION.txt`、可执行 `ROLLBACK.sh`。回滚只作用于独立源码副本，不回退业务数据库、凭据或外部动作。

初轮服务 16 项中 14 通过；两项失败分别是测试把 create DTO 的 `occurrence:null` 附加字段当成原 Item 字段，以及 HTTP 测试未配置原 header-auth 所需 `app.state.config.api.api_keys`。原输出保留，不作为最终通过。独立审查还修正了部分已读/失权批次饿死后续提示、随机历史前缀限制、跨 DND 的 claim 时序、旧 scan 覆盖时钟高水位和原日历 grant 重新绑定等分支。

本模块的受控本地 StreamChunk、认证 Job/Run/SQLite、真实 loopback HTTP、冷进程和生产 UI 证据，与真实模型、真实 Google 账户、CI/main、部署和容量验收独立。Google Calendar 仍是用户选定的首个平台；私密 Desktop OAuth 配置未到位，真实账户 OAuth/读取/写回/读回/撤销保持 NOT_RUN。

## 后续独立模块

跨工作区汇总、截止/事件驱动建议、OS/mobile push、跨设备同步、云端常驻和其余原视频能力继续按原验收推进。本轮不将完整 PA-04 或 24 项总目标标记完成。
