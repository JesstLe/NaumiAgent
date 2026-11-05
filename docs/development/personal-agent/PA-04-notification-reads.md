# PA-04：通知列表的批量读取与末次授权

2026-11-05。基线 `bf9b95a1`，分支 `codex/personal-agents-pa00`。

## 问题与证据

上一通知模块的真实浏览器暴露了列表等待较长的问题。旧列表先为计数遍历全部普通来源两次，再为当前页投影与复查；两条真实结果、其中一条未读时，`all` 请求执行 8 次来源授权，`unread` 执行 7 次。原 cProfile 另显示 Windows 路径、实例与 OwnerJournal 重复验证放大开销。本模块不通过提高客户端超时或缓存私有正文绕过这一问题。

事务目录：`.naumi/transactions/pa04-notification-reads-20261105/`。原 3117 个 tracked 文件已在首次源码修改前冻结原始 bytes、SHA256、mode 与 mtime，并保留完整源码副本。原路径绑定的旧验收数据不迁移、不重绑来源身份；基线和修改版均在同一个已完成的隔离 fixture 绝对路径读取相同持久事实。

## 实现

修改范围限 `personal/notifications.py` 的列表／本域 metadata 读取，没有新执行器、数据库 schema、权限放宽、前端产品改动或新工具协议。

1. **计数与正文分离。** `list()` 复用认证 `_summary()`：普通未读计数包括失权的 opaque 通知，不为计数读取所有普通报告来源。高优先的需处理数量仍真实授权并末次复查。计数保留 `VIEW_LIMIT` / `partial`，新增 `counts_consistency=journal_snapshot`；human actor filter 只过滤页，计数仍为原 caller 范围。
2. **短事务批量 metadata。** 一次认证 OwnerJournal read 获取原 raw page 与其 notice-read / outbox；末次快照按原事件 ID 重新读取，核对不可变 digest、workspace、receipt exact event ref、outbox actor/kind/event 以及 sealed batch member 绑定。相同事务内可复用已经解码的 batch，不跨请求复用来源 proof。
3. **保留真实来源双检查。** 初次来源投影后刷新本域 metadata，再逐项执行原 `_authorize_event` 末次检查。最后 source pass 之后没有新的本域数据库 await；不跨 await 持有 SQLite transaction 或 writer lock。Item / Job / Run / delivery / Calendar grant / Artifact 来源验证原样保留。
4. **最终重新过滤。** 其他客户端在 source await 期间确认已读时，末次 metadata 的 unread 状态决定是否留在未读页；高优先来源变为 resolved / unavailable 后移出需处理页。`all` 和未读中的失权条目保留 opaque 身份，但清正文、Agent 名称与跳转。
5. **稳定有界分页。** `next_after` 始终使用原 raw page cursor，不用过滤后最后一项推断；即使一页过滤为空仍能继续。没有扫描到填满为止的无界循环；计数窗口之外仍可 keyset 续页。

`_entry(event, caller)` 原内部接口仍供批次、claim、通知已读及高优先 summary 使用；其两项 metadata 查询合入同一短读。新增 `_project_entry` 共用同一来源授权逻辑，不重复实现来源规则。

## 语义和边界

- journal snapshot 不等于跨 Job / Run / 账户授权和人工已读的线性化全局瞬间。快照之后提交的 receipt 可在下一请求反映；没有声称即时推送撤权。
- 高优先通知密集时，需处理计数仍要核高优先来源；本模块不是任意数据分布下的 O(page) 保证，也不是容量或 SLA 验收。
- batch 的合法 pending → sealed 进度可刷新 `batch_id`，不会因普通 CAS revision 增长产生假冲突；错配、伪 parent、损坏 checksum、foreign workspace 或 owner 继续拒绝。
- 模型仍从 query 重建列表、发布实际 material bytes、保存不可变 read refs、绑定当前 session，并在下一发送重查来源。GET 不签 presentation、通知已读或原结果已读。

## 验证方式

新增 `tests/unit/test_personal_notification_reads.py`：真实临时 OwnerJournal、显式受控授权状态机、真实 Calendar 生产 mixins 和真实 material/session provenance。覆盖 page 级授权次数、末次已读过滤、撤权、游标、>1000 receipts、metadata corruption / scope / member 绑定、合法批次进度、无锁跨 await、末次 source 顺序、GET 无写以及模型下一发送。受控授权器只证明新读取结构，不替代原完整来源 suite。

真实 HTTP 使用原完成的两条 Item / Job / Run / report / delivery，读取前后核 15 个数据库的完整 schema 和 row digest，并认证原 manifest、Job / Run / delivery 与人工收据。没有 seed、worker、tick、模型调用、外部网络或原生凭据访问。冷 Engine 可能触及 PersonalAgentStore 幂等 schema 元数据，因此逻辑事实与物理字节分别记录，不声称所有 DB bytes 不变。

前后使用同一个未改的 production component bundle 和同一只读 browser driver，比较 boot、未读／全部／需处理、Agent filter、namespace 清旧正文与移动端实际 DOM 完成。浏览器业务 POST 固定为 0；后台 poll / abort 和并发状态逐项保留，少量样本不冒称 p95。

初始两个串行 HTTP 样本（不含 cProfile）中，基线 `all` 为 8878.566 / 9096.180 ms，修改版为 2797.124 / 2971.687 ms；基线 `unread` 为 7674.282 / 7446.492 ms，修改版为 2023.053 / 1708.987 ms。`all` 的实际 thread 调度由 317 次变为 136 次，来源授权为 4 次；`unread` 的调度为 74 次，来源授权为 2 次。这里只报告相同两条来源的本机样本，不推断其他规模或高优先分布。

集中回归、生产构建、最终浏览器样本、原失败、同事实比较与服务停止、copy-only 回滚和独立提交，均以事务 `VERIFICATION.txt` 的原始命令、literal output 和 exit status 为准。四产物为 `MODIFIED_FILE.py`、`DIFF_FILE.patch`、`VERIFICATION.txt`、可执行 `ROLLBACK.sh`，最终逐个重开。

## 后续

完整 PA-04 仍为 partial，原视频 24 项范围未缩减。后续先做跨 workspace 的原路由、唯一执行权与重复调度隔离，再做绑定原时区／截止证据的到期建议和人工确认动作。Google 真实账户、Calendar 浏览器、多设备、语音、电脑执行与发布验收仍独立推进，不由本模块替代。
