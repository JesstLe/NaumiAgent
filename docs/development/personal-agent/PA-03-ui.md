# PA-03 — 连接工作区与日历动作审批

## 当前实现

- 独立浅色「连接」工作区加入 Web2 主导航，`?connections=1` 可冷启动打开。
- 原 Settings 内本地 Runtime 地址与连接令牌流程保持原样；Google 连接与 Runtime 连接分离。
- 左侧连接卡显示真实账号、邮箱验证、主日历 `primary`、实际 OAuth scopes、读写权限、同步与撤销状态。
- OAuth 首次只读连接、写入升级均使用服务端 intent；升级经过独立确认，不把只读 grant 自动扩大为写入权限。
- 客户端 JSON 只经文件选择器读取并直接传给 Runtime；正文、client secret、授权 URL、token 均不写入 localStorage/sessionStorage，不打印日志。
- 日程支持真实分页、搜索、单个重复实例详情、原 Google 链接与明确来源版本。日程正文只按文本渲染。
- 日程转事项调用既有事项接口，冻结原 event revision，用户指定执行要求、Agent、一次性时间和时区。
- Agent 日历分享只读且绑定当前 specialist 的确切版本；团队不出现在个人只读分享列表，撤销保留原 grant 版本。
- 动作具有草稿、新建/修改、原 event/ETag、审批有效期、原来源 ref、人工 approve/reject、原动作 reconcile。
- approve 只调用 Runtime 的批准接口一次；前端不调用 Google 写入接口，不在批准后另发一次 provider write。
- Action 顶层 `artifact_refs/memory_refs/memory_item_id` 仅只读显示来源 chips，不进入 prepare/revise payload。资料按 Action.actor_id 与原 artifact version 导航；记忆显示原引用修订并打开原 ID 的记录，不把当前记忆当成原引用、不替换旧 grant。
- 批准确认冻结所见 action ID/revision/digest，后台更新后必须重新审阅。
- 编辑草稿有脏编辑导航与 beforeunload 保护；390px 下卡片/详情切换，1440px 下分区操作。

## 恢复身份与 UNKNOWN

`naumi:calendar-recovery:v1:<namespace>` 仅保存以下字段：

```ts
{
  request_id, operation, owner_key, workspace_key,
  resource_id?, intent_id?
}
```

- 每个 mutation 先持久化原 request ID，再发送 POST；写入 localStorage 失败时不发送网络请求。
- 响应丢失后冻结新写入，显示原 request/intent ID，通过 `GET /personal/connections/requests/{request_id}` 或原 intent GET 核对。
- 空列表、超时、503、未观测 receipt 都不代表未受理；lookup 返回 `unknown` 时原记录继续保留。
- 仅 `applied` 且原 request ID、结果 ID 匹配时接纳真实 DTO；不以新请求猜测或重建旧操作。
- OAuth 仅 `connected + connection_id` 视为成功；`exchange_unknown` 保持 UNKNOWN。
- 取消授权在 POST 前持久化自己的原 cancel request ID；响应丢失不再创建另一个 cancel request。
- 断线只保留 opaque IDs；待核对请求不保存日程正文、执行要求、客户端文件或密码。

## 来源与时区

- `CalendarSourceRef` 支持 `grant_id/grant_revision`；前端整对象透传，不截断或自行派生另一个 Agent 的授权引用。
- 操作草稿保留每个原来源 ref；另一 actor 是否具有确切来源权限由后端重新核验。
- IANA 时区通过 Intl 的真实时区规则映射，夏令时跳时拒绝，重叠时间可明确选择第一次/第二次。
- 动作 body 发明确的 UTC instant 与原 IANA timeZone；事项 schedule 发本地时间、时区、fold 与一次性 misfire policy。

## 文件与验证

- Typed DTO：`frontend/shared/src/api/personalConnections.ts`。
- API 方法：只向 `WorkbenchRuntimeClient` 新增 connection/action 方法，不修改既有方法。
- UI 与恢复状态：`frontend/web2/src/personal/connections/`。
- 自动化测试：`frontend/web/tests/unit/personal-connections.test.tsx`。
- 模块完成后集中执行 TypeScript、Vitest 与 build；真实浏览器由 Root 统一执行。

## 验证与待接续

- `pnpm --dir frontend/web exec tsc -b` 已通过。
- 新模块冻结版集中 Vitest 22/22 通过；首轮扩大相关集 330 pass / 1 fail 的 URL 异常保留在事务日志，已在新组件链接白名单内修复，未修改旧 helper。
- 22-pass 证据及其输入已保留；后续只读来源 chips 与新增两例由 Root 最终前端全组一起验证，本阶段不单独重跑。
- `pnpm --dir frontend/web run build` 已通过；既有单 chunk 超过 500KB 提示未在此模块扩大处理。
- 自审追加“新连接不重绑当前账号”与同一 React 提交批次内未知写冻结案例，均已通过。
- OAuth/provider、真实 Google 账号与运行密钥未读取、未触发。
- 1440/390 浏览器与后端集成验收由 Root 接续，未将单元 fixture 当成真实 Google 授权或写入证据。

## 最终只读错误状态接缝

- Action 的 `last_error_code` 只读安全标识映射为中文账户、来源、受理、执行或回读证据状态，不直接显示原始 code。
- `not_found`、503 和传输未知均保持“结果未知，请按原动作编号对账”，不显示未受理、不解锁批准或另发写入。未知 code 使用“行动证据待核对”。
- 来源 chips 两例、错误映射四例与 UNKNOWN 详情一例由 Root 最终全组验证，本阶段未另跑 Vitest/build；当前模块共 29 cases。


## 只读拟稿与显式写入升级切片（2026-11-05）

- 同步状态以真实后端 `not_synced/syncing/ready/error` 为准，`ready` 显示“已同步”，未知状态保留待核对；兼容旧 `synced` 等显示别名。
- 健康 connected 只读账户可新建、修订及根据原事件拟定草稿；拟稿不申请 OAuth、不提交 Google、不自动批准。修改提案继续完整保留原对象、ETag 与授权来源版本。
- 真正批准仍要求当前原账户、相同邮箱与 auth_epoch、可写权限、未过期的原 action revision/digest。只读待审批明确显示“批准前需启用日历写入”。
- 待审批详情提供“启用写入权限”，复用独立升级确认；确认固定到该动作的原账户和连接 revision，不误选侧栏其他账户。取消或连接快照变化不发 OAuth，升级成功也不自动批准。
- 当前前端仅接纳服务端明确保留 auth_epoch 的健康同身份纯 RO→RW 连续性；其他重新授权仍通过 auth_epoch mismatch 阻止旧批准。本轮没有在前端模拟或放宽该后端规则。
- UNKNOWN 原请求、本地 Runtime 断线、账户断连/需重授权/refresh_unknown、授权批次变化继续阻断对应草稿/批准入口。
- 三文件原始 SHA 与副本保存在事务 `ui-readonly-before/`。完整模块完成后集中运行全前端 Vitest 与 production build；结果将追加至本节，不将单元测试冒充真实 Google 或浏览器验收。

### 本切片集中验证结果

- 全前端 `pnpm --dir frontend/web run test:run`：**31 test files / 360 tests passed**，Vitest 14.41s，exit 0。包含本切片新增 17 cases；没有失败后的增量重跑。
- `pnpm --dir frontend/web run build`：exit 0，包含 `tsc -b`，Vite build 1.21s；已生成新的 `frontend/web/dist`。
- 测试/build 执行期间三归属输入 SHA before/after 完全相同。原始输入、完整 stdout、命令与退出状态保存在事务 `ui-readonly-before/`、`ui-readonly-full-first-record.json` 和两个 `*-output.txt`。
- 自审确认：RO 拟稿与 provider 写权限分离；待审批升级复用原确认且多账户目标固定；真实写入升级完成后仍需独立人工批准；未知请求与 Runtime/账户断线不解锁。
- 本批存在既有 jsdom canvas 提示与生产 chunk 大小提示，测试/build 均 exit 0；没有扩大到 canvas 依赖或打包重构。浏览器、真实后端连续性与外部 Google 仍由 Root 的独立 HTTP/provider 层级验收。
