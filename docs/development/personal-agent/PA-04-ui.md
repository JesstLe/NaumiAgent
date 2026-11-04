# PA-04：周期计划界面与共享客户端

日期：2026-11-05

## 本轮范围

实现一个独立的周期计划纵向切片，不改变 PA-01 单次事项的 `now/once` 合同，不把旧会话提醒的 cron 列表等同于角色级执行。每日安排、跨工作区汇总、截止时间跟进、事件建议、通知合并/勿扰仍由 PA-04 主记录保留后续范围，本轮没有添加这些能力的静态入口。

## 领域与界面

- Web2 侧栏和视图菜单打开真实“周期计划”工作区，支持 URL `?routines=1`、刷新、浏览器前后导航和工作区切换。
- 规则 `active/paused/cancelled/closed` 与子事项执行状态、真实 delivery 回执分别显示。规则启用、已触发、已执行、已送达不共用一个“成功”标志。
- 用户可选择现有主 Agent 或 active 的个人 Agent；名称与版本来自真实档案，不预设角色，不在执行时新建身份。停用 Agent 和团队不作为可新选择的执行者。
- 单次、cron、交易日使用同一服务端预览。交易日日历与覆盖区间只来自 `/context`；上下文无日历时禁用该选项，不伪造未来交易日。
- 明确 IANA 时区、UTC 起止边界、夏令时重复小时、错过补执行、重叠与并发策略。创建、修改先显示服务端真实近期触发时间、次数和估算额度，再显式确认 `preview_id + definition_digest`。
- 修改绑定原 routine/revision 的预览；只改变后续规则，已受理子事项保留原版本。暂停和取消确认明确说明仅控制后续计划，不声称终止已运行的 Job。
- 显式额外执行使用独立操作，占用次数与额度，不重放未知原执行。
- 显示已开始/预留/剩余次数，已结算估算/预留/剩余额度；`unknown_count` 单独显示并保留原额度。缺失结算费用显示“待核对”，不补零。核算口径明确为本地单价估算上限，而非服务商最终账单。
- 执行历史和修订历史按服务端游标分页；原 occurrence、child item、Job/Run、报告、送达链可重开查看。查看不生成任何人工已读。
- 全部采用浅色布局、中文操作和功能性确认，不添加 tagline。

## 共享客户端合同

新增 `frontend/shared/src/api/personalRoutines.ts`，原始定义与公开规范化定义分离：

- raw `assignee_id/version`、`input_refs/directories` 不混入 route、schema_version、已解析日历证据。
- public `schema_version:1`、`assignee`、`inputs`、route 和日历/时区证据仅作真实回读。
- epoch 是独立的 32 位 hex 身份字符串，不是序号。
- `max_executions=0` 或总额度 0 表示禁止新执行，不表示无限。
- preview/create/revise/control 均携带稳定 request_id。

`WorkbenchRuntimeClient` 新增 context/list/get/preview/create/revise/control/history/occurrences/request 查询方法。读请求使用 `cache:no-store`，资源 ID 与游标编码，写请求保持原正文、摘要和 CAS。

REST 路径均在 `/personal/routines` 下；具体合同与状态机以 [PA-04 主记录](PA-04.md) 和服务端实现为准。

## 未知结果与隐私

`usePersonalRoutines` 在任何写请求前先持久化仅含以下身份的记录：

```text
version, operation, request_id,
routine_id?, preview_id?, expected_revision?, expected_digest?
```

事项正文、instructions、材料引用、目录路径和完整 definition 均不写入 localStorage。

503、响应丢失、列表缺行、404 或 pending/unknown receipt 均不推导为“未受理”，也不自动重发 POST。用户通过“核对原请求”只执行 GET `/requests/{request_id}`，核验 operation、result_kind、原资源 ID、定义摘要和 base revision 后清除本地身份记录。重新打开已应用的 preview 会从服务端真实回执重建确认界面。

被损坏或含多余正文键的本地 pending 记录阻止新写入，等待明确处置；存储失败发生在网络传输之前。跨 tab 改变的身份不会被旧响应清除。停止核对有独立确认，不宣称撤销已受理的规则或执行。

只读列表刷新与写入错误分开，避免异步 GET 清掉用户需要处理的提交错误。

## 现有组件复用

- `ItemEditor.tsx` 仅导出既有 `MaterialPicker` 并缩小其 API Pick。周期计划直接复用同一精确材料选择、64 项约束、actor 查询和焦点管理。
- `PersonalItems.tsx` 仅导出既有 `ExecutionCard`，对子报告/Run 组件缩小 API Pick；执行报告继续核验 actor、版本、SHA、grant/revision，不读 latest 替代旧版本。
- 编辑器复用 `artifactNavigationEvent`：关闭、换工作区、浏览器导航、打开报告时保护未保存修改；beforeunload 同时保护离开页面。
- 旧单次事项和旧 `/schedules` 会话提醒入口及合同保持原语义。

## 文件

- `frontend/shared/src/api/personalRoutines.ts`
- `frontend/shared/src/api/WorkbenchRuntimeClient.ts`
- `frontend/web2/src/Web2.tsx`
- `frontend/web2/src/personal/routines/usePersonalRoutines.ts`
- `frontend/web2/src/personal/routines/RoutineEditor.tsx`
- `frontend/web2/src/personal/routines/PersonalRoutines.tsx`
- `frontend/web2/src/personal/routines/routines.css`
- `frontend/web2/src/personal/items/ItemEditor.tsx`
- `frontend/web2/src/personal/items/PersonalItems.tsx`
- `frontend/web/tests/unit/personal-routines.test.tsx`

## 集中验证记录

首轮完整前端 `pnpm --dir frontend/web run test:run`：371 passed / 4 failed / 375 total，exit 1。新增周期模块的三个失败涉及测试 readiness、HTTP mock URL 和真实的异步 GET 清掉写错误；既有 memories 分享测试的一个失败单独保留。本轮未以修改 memory 业务逻辑掩盖该失败。

首轮 `pnpm --dir frontend/web run build`：exit 2。三处 TypeScript 错误分别为复用组件 API Pick 未继续缩窄、联合类型在闭包内失去缩窄；均根据实际输出修复。原日志保存在 `.naumi/transactions/pa04-recurring-20261105/ui-test-first-output.txt` 与 `ui-build-first-output.txt`。

第二轮集中结果为 32 files / 379 tests 全部通过，exit 0；TypeScript 与生产构建通过，exit 0。此后对齐最终接口边界：未知 receipt 的 operation 可为 null；max_catch_up 的最小值与服务端一致；全部次数、并发、费用、恢复上界与合同一致；修复超远未来测试预览的浏览器 timer 溢出警告；选择个人 Agent 同步其模型等级与工具交集。

冻结后的最终集中验证：

| 命令 | 实际输出 | 退出状态 | 记录 |
|---|---|---|---|
| `pnpm --dir frontend/web run test:run` | `Test Files 32 passed (32)`；`Tests 379 passed (379)`；`Duration 15.15s` | 0 | `ui-test-frozen-output.txt` |
| `pnpm --dir frontend/web run build` | TypeScript 无错误；`✓ built in 1.10s` | 0 | `ui-build-frozen-output.txt` |

日志位于 `.naumi/transactions/pa04-recurring-20261105/`。本模块新增 19 项前端测试，覆盖只存 opaque 身份、响应丢失与原请求 GET 对账、错误身份、损坏记录、存储失败、跨 tab 身份改变、预览与原版本绑定、显式确认、过期预览、输入修改失效、真实日历上下文、精确材料组件复用、未保存保护、未来触发控制、未知费用、原执行与历史分页、REST 方法/编码/no-store。

最终日志中仍有既有 jsdom HTMLCanvasElement 未实现提示，以及生产 bundle 大于 500 kB 的分块建议；没有将提示当作真实 Canvas 或浏览器验收。

验证层级为 **FULL_FRONTEND_LOCAL**。组件测试、生产构建、真实 HTTP/持久化、真实到期/冷重启、浏览器与 provider 属于不同验收层；本文件不会以某一层通过推导另一层完成。本轮前端实现未启动浏览器或服务，未执行 provider 调用；后端真实连通、真实到期与冷重启由主事务独立核验。
