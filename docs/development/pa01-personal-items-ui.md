# PA-01 事项与结果收件箱 UI

## 实现边界

- 基线：`2e8e6c372690772c532a143ff1d4205d08947d7f`。
- 仅接入真实 Personal Items API；未修改 Library 的权限、版本解析和资料写入逻辑。
- 通用 Agent：主 Agent 默认，只列出当前启用的实际个人 Agent，不预置岗位，不把团队伪装为独立执行者。

## 完整操作链

1. 侧栏「事项」进入独立浅色工作区。进行中 / 已暂停 / 全部与搜索走服务端分页；当前机械执行状态使用 `occurrence.status`，不把 item 控制状态当作 Job 结果。
2. 创建 / 修改编辑器：名称、执行要求、真实 Agent 版本、标签、立即 / 指定时间、IANA 时区、夏令时 fold、错过时间策略、实际已授权材料与目录、可选执行限制。
3. 材料选择器只查询所选 Agent 的 Library，保留 `artifact_id/version/sha256/grant_id/grant_revision`；切换 Agent 清空旧选择；64 项上限明确报错，不截断，不自动分享。
4. 修改使用 `expected_revision`。准备后的定义不再显示编辑入口。暂停 / 恢复走真实 CAS；取消、立即运行、再次执行需要明确确认。再次执行只对已知终态或未执行的取消/关闭项开放，提交新的 `now` schedule。UNKNOWN 保留原执行身份，无再次执行入口。
5. 详情显示真实事项修订、时间/时区、输入、执行身份、答案、报告、历史定义及历次执行。
6. Run 记录仅 GET 执行会话历史，按原 `run_id` 精确选择；不调用 load/create/send，不从 latest Run 推断完成。
7. 结果收件箱：未读 / 全部。查看、刷新、打开报告、查询 Run 均不产生已读写入；「标记已读」才发送服务端投递 ID / SHA-256 和原 request_id，随后 GET 核对真实 read receipt。
8. 报告卡先以执行 Agent 身份核对精确版本、内容 hash 与授权修订，打开时沿原 Library 导航传入该 actor + exact version；漂移或撤销会显示错误而不是改读主 Agent / latest。

## 重试与未保存保护

- 每次写入先把完整 request_id 与 payload 持久化到按工作区隔离的 localStorage。存储失败时零网络写入。
- 503、响应丢失、缺行、读回执未确认、响应身份不一致均保留原完整请求；读取/轮询/重挂载不会生成新身份、替换 payload 或清空它。
- pending 状态锁住新写入，提供「原样重试」及完整原请求查看；放弃重试需要明确确认，且不会声称撤销服务端已受理动作。
- 读取错误与写入错误分开保存；刷新成功不会抹掉待核对写入错误。
- 未保存编辑监听现有统一导航事件与 beforeunload；返回/切换面板/切换 Agent/选择会话/打开其他项目会先确认，浏览器返回取消时恢复当前 URL。
- 材料对话框支持 Escape、焦点限制和关闭后焦点恢复。移动窄屏以详情覆盖列表并提供返回操作。

## 集中验证

基线：

```powershell
pnpm --dir frontend/web exec vitest run tests/unit/personal-artifact-library.test.tsx tests/unit/personal-agents.test.tsx
# Test Files 2 passed (2); Tests 49 passed (49); exit 0
```

修改后：

```powershell
pnpm --dir frontend/web exec vitest run tests/unit/personal-items.test.tsx tests/unit/personal-artifact-library.test.tsx tests/unit/personal-agents.test.tsx
# Test Files 3 passed (3); Tests 63 passed (63); exit 0
pnpm --dir frontend/web run build
# TypeScript + Vite production build; exit 0
```

最终集中回归：

```powershell
pnpm --dir frontend/web exec vitest run tests/unit/personal-items.test.tsx tests/unit/personal-artifact-library.test.tsx tests/unit/personal-agents.test.tsx tests/unit/personal-collaborations.test.tsx tests/unit/personal-peer-messages.test.tsx tests/unit/personal-team-turns.test.tsx
# Test Files 6 passed (6); Tests 142 passed (142); exit 0
pnpm --dir frontend/web run build
# TypeScript + Vite production build; exit 0
```

新测试包含 15 个事项测试（含名称 120 字、自定义预算 100 美元、超时 3600 秒的原生表单边界）：持久 UNKNOWN/create/control/read 精确重试、损坏身份、localStorage 写失败零提交、Agent/team 过滤、授权材料精确版本、Agent 切换隔离、时区/fold、未保存导航保护、UNKNOWN 无重启、明确再次执行、GET 无 ACK、报告授权漂移、服务端筛选，以及实际 fetch + MSW HTTP 路由/token/payload/409 合同检查。

证据：`.naumi/transactions/pa01-20261104/ui-baseline-tests.txt`、`ui-modified-tests.txt`、`ui-typecheck.txt`、`ui-regression-tests.txt`、`ui-build.txt`。源文件原件与 SHA-256 存在 `original/frontend/...` 和 `ui-baseline-manifest.json`；事务总回滚由 PA-01 主流程统一打包和演练。

## 真实浏览器验收（2026-11-04）

- 实际生产前端 + 实际后端 `http://127.0.0.1:18770/web2`，无 MSW、无页面数据替身。
- 管理链 10 项通过：使用既有 Echo，真实授权 `expenses.csv` 精确引用，创建唯一 2035 远期事项 `pi_8e024635b17c4441ad29e54da792e63a`，201/rev1 → 暂停/rev2 → 恢复/rev3 → 编辑/rev4 → 明确取消/rev5；筛选、搜索、关闭重开、reload、未保存导航与 390px 布局均通过。未点击立即运行或再次执行。
- 收件箱链 7 项通过：用户已授权的三条真实 provider 原结果均存在报告、Run 与 delivery；GUI 查看、刷新、重挂载后仍全部未读。Echo 的原 Run `70ca2b4eaf95` 实际 9 个步骤可见；报告 `art_88664dc9083e4c5dbf710b79ceb110a1` v1 以真实 Echo actor + exact version 沿 Library 打开。
- 唯一显式已读：`pio_6471609ce2b97d607f373c4f9dd16cd9`，原 request_id `c63bb17c047e4c5a956c0880ca078387`，实际人类回执 `human-read-096e54b5aaf0afd61f38679c13ecc46b`，可信通道 `management_web`。原工作/学习两条仍未读。
- 两轮浏览器 `page_errors=[]`、`denied_mutations=[]`；checkpoint `inflight=null`，无未知请求重放，无新 Agent、无新增 paid 执行、无 execution session load/create/message 写入。
- 管理 1440/390、实际报告页、收件箱 1440/390 五个 PNG 均真实存在并打开检查。

证据：`ui_live_acceptance.py`、`ui-live-prepared.json`、`ui-live-checkpoint.json`、`ui-live-management-report.json`、`ui-live-inbox-report.json`、`ui-live-management-output.txt`、`ui-live-inbox-output.txt`。脚本持久记录原请求和回执；有未确认 mutation 时停止，不以列表缺行创建新身份或重放原创建。

最终前端集中回归：`ui-final-regression-tests.txt` 为 6 文件 / 142 项通过，`ui-final-build.txt` 为 TypeScript + Vite 生产构建 exit 0（通过绝对 `D:/DEV/pnpm/pnpm.exe` 执行）。cold restart、报告数字的独立核验和总回滚由 PA-01 主流程单独记录，本 UI 验收不替代那些证据。
