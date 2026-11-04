# PA-06 团队独立对话与任务界面

日期：2026-11-04

## 实现

团队继续使用用户创建的真实档案与独立日常会话，不预置团队、成员名称或业务领域。
目录直接打开现有 `openPersonalSession/currentPersonalAgent` 身份链。

### 同一个消息输入

- 所有团队日常消息通过 `POST /personal/team-turns` 接纳，不调用普通聊天 SSE。
- 团队没有显式 `personal_team` 时仍走同一持久入口，由后端完成普通对话，不在前端补造成员分工。
- 明确配置协调能力后，成员与工具范围从当前真实成员关系递归展开。
  默认仅选择真实目录标记为只读的工具及已分配的 `personal_message`；写入权限需要明确勾选。
- 取消勾选成员表示本次不分配给该成员；已选择成员但工具列表为空表示仅对话。
- 成员版本变化不静默升级已选授权；停用、缺失与循环关系有明确反馈。
- 共享资料与已选择附件仅属于本次请求；团队不继承所有成员的工具权限。

### 请求与响应身份

- 首次发送冻结 `source_session_id/team_id/team_version/request_id/content/context/source_ids/member_grants`。
- 不确定 POST 失败后，保留原载荷并原样重试，不凭错误文字或一次列表缺行判断未受理。
- 待核对请求按 API 地址、团队和日常会话隔离保存在本地，重挂载后仍复用原身份。
- 列表只有返回同一原请求的确切记录，才解除待核对状态。
- 用户显式“放弃重试”需要确认，且说明该动作不取消已受理任务；后台任务与任务记录继续存在。
- 返回的团队 ID、团队会话、请求 ID、职责版本、正文与资料均核对；身份或结果摘要冲突保留原记录并显示错误。
- 有界列表之外的历史任务依据真实消息中的 `metadata.team_turn_id` 调用单任务 GET 取回，最多四个并发，终态缓存；不以最新列表缺行抹掉旧任务卡。
- 已核实终态、结果消息与结果摘要不会被旧轮询响应退回成处理中；刚接纳的任务也不会因较旧列表暂缺行而消失。

### 任务与真实执行详情

- 原用户消息下显示对应任务卡，按真实规划、成员协作、汇总阶段显示实际协作 ID。
- 显示成员历史名称/版本、真实任务状态、独立 Run 链接和执行用量；不把成员 token 混进协调器助手气泡。
- 新任务接纳后可继续输入并排队；关闭页面不触发取消。
- 停止操作绑定已接纳团队任务，提供确认；不只终止浏览器连接，也不把单阶段取消冒充整次任务取消。
- 详情只使用该团队任务提供的 phase 清单，复用原协作授权/问题 UI，不查询全工作区历史。
- 权限和问题继续绑定实际成员 Session/call/question，成员执行记录保留只读入口。
- 权限完成去重使用 `step_id/session_id/call_id`，避免两个接收方相同 call ID 相互遮挡。
  旧根回执缺 session 字段时只推断对应根 Session，不使用会匹配其他 peer 的通配键。

### 后台交付与对话刷新

`refreshConversation(expectedSessionId)` 只更新当前会话消息。
它在读取前后校验 active ID、会话 generation 与刷新序号，不清用户草稿、附件选择或新会话内容。
团队历史刷新失败不会伪装成任务失败，结果仍留在真实任务记录中可再次核对。
前端不向当前聊天手动拼接后台汇总文本；结果由后端 append-once 写入原团队会话，再从实际消息 API 读回。

团队聊天中的普通消息重生成/历史编辑入口不沿用旧 generic POST，避免绕过团队请求身份与历史追加约束。

## 本地验证

事务目录：`.naumi/transactions/pa06-teams-ui-20261104/`。

| 层级 | 命令 | 字面结果 | 退出状态 |
| --- | --- | --- | --- |
| BASELINE | `pnpm --dir frontend/web test:run tests/unit/personal-agents.test.tsx tests/unit/personal-collaborations.test.tsx ../shared/tests/WorkspaceProvider.test.tsx` | 3 files / 56 passed | 0 |
| 模块 | 七个个人目录、协作、peer、team API/DOM 与共享 Workspace 测试文件 | 7 files / 101 passed | 0 |
| 历史链接补齐 | team hook/DOM、团队 API 与共享对话刷新三个测试文件 | 3 files / 23 passed | 0 |
| 最终全量前端 | `pnpm --dir frontend/web test:run` | 25 files / 201 passed | 0 |
| 最终构建 | `pnpm --dir frontend/web run build` | TypeScript 成功；`✓ built in 731ms` | 0 |
| Chrome fixture | `node .naumi/transactions/pa06-teams-ui-20261104/visual.mjs` | 1440 / 768 / 390，无横向溢出，无 page error；权限使用确切成员 Session；未查询工作区历史 | 0 |

日志保存为 `baseline.txt/module-tests-fixed.txt/history-link-tests.txt/full-tests-final.txt/build-final.txt/visual-final-source.txt`。
最初模块旧 fixture 的根 permission_resolved 缺 Session，触发两个待授权按钮；已通过明确根 Session 兼容处理，原失败日志保留。
首次视觉脚本未展开工具详情导致只读默认断言定位不到隐藏复选框，修正后通过；未将该定位错误当作权限逻辑通过证据。

视觉产物为真实组件和显式 fixture，证明布局与前端身份链，不冒充真实模型、真实业务执行或最终 HTTP 集成验收。
后端、真实模型、跨重启执行及统一源码回滚由父任务整体验证。
构建保留既有大包提示；JSDOM 保留已有 Canvas getContext 提示，201 项断言通过。

## 文件归属

- 共享客户端：团队任务类型、四个 API 方法、保留状态码/错误码的 `WorkbenchRequestError`。
- 共享控制器：受来源保护的 `refreshConversation`。
- `Web2.tsx`：团队消息分流、原输入与真实任务卡集成。
- `personal/useTeamTurns.ts`：请求身份、作用域、轮询、确认与取消。
- `personal/TeamConversation.tsx` / `team-conversation.css`：成员与权限、任务卡、受限阶段详情。
- `PersonalAgents.tsx`：按 `kinds` 筛选协调工具，不默认授予。
- `Collaborations.tsx`：受限 phase 列表、指定初始阶段和确切权限去重键。
- `PeerMessages.tsx`：复用范围编辑器的团队成员标签。
