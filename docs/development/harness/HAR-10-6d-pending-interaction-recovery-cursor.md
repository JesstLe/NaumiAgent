# HAR-10.6d Pending Interaction Recovery Cursor

## 目标

消除 durable interaction 启动恢复的固定 50 条盲区。恢复扫描必须在继续复用 HAR-10.6c 四级公平调度的
同时稳定翻页；新写入问题不能挤入正在进行的扫描，Store 竞争也不能让 cursor 越过尚未确认的记录。

本切片只补交互恢复 cursor 和 New UI/TUI 消费闭环，不实现跨主机 push、跨 Goal 搜索或 Harness/Pursuit
跨库事务。

## Authority 与 cursor

`HarnessStore.list_pending_interactions_page()` 是 pending recovery 的唯一分页原语：

1. 第一页固定当前 pending 集合的 SQLite rowid 高水位；后续新记录留给下一轮扫描；
2. cursor 分别保存 `critical/high/normal/low` 四个 FIFO lane 的位置和 4:2:1:1 调度位置；
3. 每页继续上一页的公平周期，不因页边界重新偏向 critical；
4. cursor 绑定 canonical workspace、可选 subject kind/ID 和版本，subject ID 只保存 SHA-256；
5. envelope 使用 canonical JSON 摘要校验，篡改、跨工作区或跨 subject 复用均失败关闭；
6. cursor 不是第二份持久状态。宿主崩溃后从第一页重建扫描，authority 仍是 SQLite interaction/event chain。

`list_pending_interactions()` 保持兼容，并明确退化为第一页读取。历史账本的 newest-first cursor 与本 cursor
仍是两个不同协议，不能互换。

## Runtime 故障语义

`DurableInteractionAuthorityClient.recover_pending()` 返回 `next_cursor`：

- expire/takeover 成功后推进；
- live foreign owner 被跳过，但保留最短 lease retry 时间；
- transition conflict 或领域校验竞争会返回输入 cursor，并要求 0.5 秒后重试同一页；
- 读取或 cursor 校验失败直接向宿主报错，不伪装为空队列。

这样 crash window 最多造成幂等重读，不会造成 cursor 已推进但记录从未处理。

## New UI 与 TUI

- New UI Bridge 最多同时绑定 50 张恢复卡片；若 snapshot 还有后续页，cursor 留在 Python Bridge 内存中；
- 回答、取消、超时或普通实时卡片结束后释放一个槽位，并从同一 cursor 补入下一条；
- 未满 50 条时 Bridge 可立即继续下一页；扫描结束后才按累计的最短 foreign lease 安排下一轮；
- Textual TUI 处理完当前有界批次后继续下一页；同页 conflict 先退避，再重试同一 cursor；
- 两端仍通过同一个 `DurableInteractionAuthorityClient` 执行 expire/takeover fencing，前端不解析 cursor。

## 验收证据

- 真实 SQLite 中混合四级优先级的 12 条记录按 5 条分页，跨页保持公平周期且无重复/遗漏；
- 第一页后新建的 critical interaction 不进入既有 snapshot；
- cursor 跨 workspace、跨 subject 和字节篡改均失败关闭；
- runtime 以 7 条批次恢复 23 条真实记录，每条只出现一次；
- 模拟 takeover transition conflict 后保持同一 cursor，重试成功才推进；
- New UI 以 51 条真实 authority 验证最多 50 张卡片，回答一张后自动补入第 51 条；
- 现有 Bridge 与 Textual TUI expired-owner replay 回归通过；
- 仅运行 interaction Store、Bridge/TUI 指定测试、Ruff 与 Python 编译，不运行全量测试。

## 保留边界

- cursor 不跨进程持久化；重启从第一页重建，不依赖陈旧宿主状态；
- 多实例新增 interaction 仍靠启动扫描和 lease 复查发现，主动通知属于 ARC-06；
- interaction/Pursuit 跨 Store 原子提交、正文静态加密仍分别属于 ARC-05/08；
- 本切片不改变 priority，也不允许 critical 绕过 permission、owner epoch 或用户决定。
