# HAR-10.6c Durable Interaction Priority Scheduling

## 目标

在 HAR-10.6 durable interaction authority 上增加跨 New UI、Textual TUI 与重启恢复一致的排队优先级。
优先级只决定“下一个展示谁”，不授予权限、不跳过 Harness fencing，也不打断用户正在回答的问题。

本切片只完成 interaction 调度闭环；不把普通对话 `/send-now`、Agent Job scheduler 或跨主机通知揉入同一实现。

## 权威合同

- 闭集：`critical`、`high`、`normal`、`low`；缺省为 `normal`，未知值失败关闭。
- 新 interaction 使用 record schema 2，`priority` 是问题的不可变字段，状态迁移不得改写。
- Harness Store v24 在快照行增加受 CHECK 约束的 `priority` 列，创建、读取与事件链末端交叉校验。
- schema 1 记录读取时投影为 `normal`；其 canonical JSON 主动排除新增字段，因此旧 payload 字节与 SHA-256
  均不变化。v23 数据库只新增默认列和索引，不重写历史 interaction event。
- `critical` 仍只是调度提示。它不能绕过 Permission、owner/epoch/sequence fence、Pursuit checkpoint 或
  Evolution 审批规则。

## 公平调度

四个 lane 各自保持 FIFO，lane 间采用固定 `4:2:1:1` 周期：

`critical → high → critical → normal → critical → high → critical → low`

选择时跳过空 lane。这样 critical 获得一半展示机会，同时 normal/low 在持续高优先级流量下仍有确定槽位。
当前已显示的卡片或 Modal 不被抢占；轮转只在它回答、超时或取消后发生。

Harness recovery 对每个 lane 做有界 oldest-first 查询，再以相同周期合并最多 50 项。每条记录在进入结果前仍
逐条验证完整事件哈希链，SQL 元数据不能替代 authenticated payload。

## 表面接入

- `request_user_input` Tool 可声明优先级，并在 schema 中解释四级用途；模型未声明时为 normal。
- Evolution Decision escalation、角色审批与 Principal governance 使用规范化 high，而不是调用层临时覆盖。
- New UI 严格校验协议字段，保留当前输入，使用持久轮转 cursor 选择下一个卡片，并以语义颜色显示优先级。
- Textual TUI 用 condition-based admission queue 替代 Lock 的隐式唤醒顺序；Modal 同样显示中文优先级。
- Goal interaction ledger/detail 投影 priority，用户能看见恢复问题为何先后排序。

## 验收标准

- 旧 schema 1 payload 在 v24 代码中恢复后 canonical JSON 与 digest 完全相同；
- v23 数据库迁移后旧 interaction 为 normal，事件 payload/digest 未重写；
- 四个 lane 同时有积压时，前 8 个选择严格符合 4:2:1:1，且 lane 内 FIFO；
- New UI/TUI 当前 low 问题已显示时，新到 critical 不抢占；当前问题结束后才按轮转选择；
- Node 拒绝未知 priority；Bridge/TUI golden request 都公开同一规范字段；
- Evolution 高价值人工决定重算 exact request 时仍与持久 record 一致；
- 只运行 interaction Store/runtime、Evolution request、New UI protocol/state 与 TUI interaction 小模块测试，
  不运行全量测试。

## 明确不足

- HAR-10.6d 已补齐独立 pending recovery cursor；历史账本 cursor 与 recovery cursor 仍是两个不可互换协议。
- 多主机只有 SQLite authority 排序，没有 ARC-06 push notification；新 host 仍通过启动/租约复查发现问题。
- 用户暂不能在运行中手动修改一个已创建问题的 priority；priority 是不可变提案事实。
- 本切片不是跨域统一 scheduler。普通对话、Agent Job、Browser 与 Sandbox 继续使用各自 admission authority。

下一步重新比较 HAR-10.7 长驻 Worker pool、公平 Job scheduler、HAR-10.8 terminal outbox 控制动作与
CC-03 用户可见行为，选择最小依赖切片；不顺势展开完整 ARC-06。
