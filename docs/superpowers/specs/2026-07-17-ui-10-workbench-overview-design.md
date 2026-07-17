# UI-10.2 Workbench Overview 设计

## 目标

让 `/workbench` 打开真正可见的全屏只读 Overview，直接消费 UI-10.1 的权威快照，回答用户当前
目标是什么、正在做什么、谁在负责、变更落在哪里、验证是否通过、风险在哪里。页面不得调用模型、
执行 Git、创建任务或根据缺失字段编造结论。

## 信息层级

1. 页头：Workbench、revision、更新时间、任务/worktree/待审/失败计数。
2. 当前目标：active mission 的标题、goal、状态；没有 active selection 时回退到首个 active/planning。
3. 当前任务：subject、description、状态、owner；owner 优先 task.owner，其次 active lease agent。
4. 变更载体：issue 的 branch、worktree、PR、expected artifacts；没有真实数据时明确“尚未绑定”。
5. 验证：当前任务最近的 validation run；显示命令、状态、退出码、耗时，不展示完整原始输出。
6. 风险：issue risk level、blocked_by、当前任务 failure、waiting approval；失败与高风险使用红色，
   待确认/中风险使用黄色，运行中使用青色，完成/通过使用绿色。

## 布局

- `>=120` 列：目标/任务在左，变更/验证/风险在右，中间语义分隔线。
- `<120` 列：按目标→任务→变更→验证→风险纵向排列。
- 所有行使用 ANSI-aware 裁剪或换行，80/120/200 列均不得溢出。
- 页面高度不足时优先保留页头、状态、目标、当前任务和首条风险；次要列表按数量折叠。
- 0 数据显示可行动空状态；100 个 worktree/review 只显示权威 counts 和当前对象，不展开全部列表。

## 路由与键盘

- `/workbench` 保存对话锚点、切换 `route.name=workbench`、发送只读快照请求。
- `r` 刷新；`Esc` 返回 conversation 并恢复此前 timeline 的 scroll/follow-tail。
- Workbench 路由不写入 UI session snapshot；新进程和显式 resume 不会继承旧页面选择。
- resume 时若用户当前正停留在 Workbench 页面，重置旧快照并请求新会话的权威快照。

## 边界

本切片不实现 Worktrees/Reviews/Timeline tabs，不做选择游标、详情折叠、approve/reject/cancel，也不改变
TUI fallback。这些分别由 UI-10.3..7 承接。

## 验收

- loading、empty、ready、error 四态都可见，显式刷新同 revision 不会卡住。
- 目标、状态、owner、branch/worktree/PR、验证、风险均来自 snapshot 对应字段。
- 80/120/200 列与中文宽字符行宽受限；0/1/100 counts 不崩溃且重要信息不被次要列表淹没。
- `/workbench` 不产生聊天提交；`r` 只读刷新；`Esc` 恢复原 timeline 锚点。
- 真实 Store→Service→Bridge→Node→renderer 输出含真实 mission/task/worktree/approval/validation 状态。
