# PA-02 可信个人记忆界面

## 交付模块

基线 `4f7d86a451cd38b5fbc84cf8e090fac7768ee63d`。本模块只覆盖共享 TypeScript 客户端、Web2 全屏记忆界面、对应测试与文档；不修改底层 Service/Store，不新增模型调用，不启动服务，不改 Logo 或全局配色。

界面沿用浅色布局。所有持久 Agent 都以 Agent 身份出现，不预置研究、运营、内容或投资人员模板，也不把团队身份视作成员集合。

### 已实现

- 主导航「记忆」进入完整列表/详情；地址记录实际 actor、范围、事项与记忆 ID，刷新与前进后退沿用真实地址。
- 个人/工作区/事项三种范围及当前授权并集；真实事项分页选择，Agent 身份选择不跨身份读取私人记录。
- 已确认/待确认/冲突/全部状态；冲突由服务端过滤后分页，前端不猜测其他页面记录。
- 手动创建、人工纠正、拒绝、删除；原修订和正文 SHA 参与 CAS。手动保存不承诺无条件成为已确认记录。
- 候选来源和人工采纳是两个事实：人工确认不会把 assistant/external/legacy 来源改写成 user。
- 冲突详情读取确切 active_ref；确认替换携带原 active_ref。来源失效、删除和刷新时清除旧正文、来源、编辑缓冲与详情。
- 分享绑定确切记忆 ID/修订/SHA 和实际接收 Agent/版本；撤销绑定确切 grant/版本。收到的分享内容无修改、再次分享或删除按钮。
- 修订/分享记录、原始 locator、摘要与资料版本按需展开；记忆页不将历史执行会话注入聊天、不自动发送消息。
- 实际服务端授权 JSON 导出；本地刷新或切换后迟到的旧导出结果不下载。
- 只有显式打开「旧记忆」才访问 legacy；导入只成为待确认候选，保留 legacy/unknown 来源，不自动迁移成个人记忆。
- 错误、加载、空结果、分页、移动端列表/详情、二次确认、脏编辑离开确认、Escape、焦点循环与返回触发控件。
- 私有 GET 使用 AbortController/代次校验；离开 Agent 后迟到的 prepare 不自动 apply，已进行中的 apply 结束后也不改写新页面 URL。
- 12 秒和窗口焦点刷新；跨标签 storage 事件重新核对 pending 并刷新授权数据。

## 持久写入与草稿清理

所有 GUI 写入共用 `prepare → 原 ID GET → apply → 原 ID GET durable receipt`。指纹是 recursively sorted canonical UTF-8 JSON 的 SHA-256，独立测试使用 Node SHA-256 对照，不依赖同一实现自证。

localStorage 只允许：`version/request_id/owner_key/workspace_key/actor_id/operation/fingerprint`。不保存 topic、content、source、payload 或其他正文。键绑定 API namespace、owner、workspace、actor。

原请求在 503、丢失响应、缺失 GET 回执、指纹不一致、空目录、重载后均保留同一身份。404 不表示未受理，也不重新 POST 原正文或另造请求 ID。

「清理草稿」必须经过用户二次确认：

1. GET 原请求并核对 actor/workspace/operation/fingerprint。
2. POST `/personal/memory-requests/{id}/discard?actor_id=`，body 仅 `{expected_fingerprint}`。
3. 再 GET 原 ID 的持久结果。只有有效终态才移除本地 opaque token。
4. `erased` 且无结果时显示「未提交的原记忆草稿已清理」；`applied` 恢复当前结果并明确不撤销已提交操作。结果若为 deleted tombstone，不声称记忆仍存在。

清理草稿不是记忆删除。已提交记忆仍通过原 delete CAS 操作删除。损坏的 localStorage token 仅提供「清理本地记录」，提示不改变服务端状态，不把本地清除伪装成服务端清理。

## 公共客户端

`WorkbenchRuntimeClient` 增加 12 个方法：context、list、get、search、history、grants、export、legacy、prepare、request、apply、discard。认证和请求超时复用原 fetch。所有 GET 都为 `cache: no-store`；POST 不接受 caller/source-role/trusted-channel 等旁路字段。

语义检索 limit 为 10（实际接口上限 20），共享搜索输入上限 200。列表 limit 30。检索依据显示实际 API diagnostics，不把词法命中表述成语义验证。

## 文件

1. `frontend/shared/src/api/personalMemories.ts`
2. `frontend/shared/src/api/WorkbenchRuntimeClient.ts`（窄增量）
3. `frontend/web2/src/Web2.tsx`（导航窄增量）
4. `frontend/web2/src/personal/memories/usePersonalMemories.ts`
5. `frontend/web2/src/personal/memories/MemoryWorkspace.tsx`
6. `frontend/web2/src/personal/memories/MemoryEditor.tsx`
7. `frontend/web2/src/personal/memories/memory.css`
8. `frontend/web/tests/unit/personal-memories.test.tsx`
9. 本文档

## 验证记录

记录目录：`.naumi/transactions/pa02-20261104`，每次命令有独立 `*-record.json` 和 `*-output.txt`，原始失败不覆盖。

### 基线

```powershell
.venv\Scripts\python.exe .naumi/transactions/pa02-20261104/ui_run_recorded.py --label ui-baseline -- D:/DEV/pnpm/pnpm.exe --dir frontend/web exec vitest run tests/unit/personal-items.test.tsx tests/unit/personal-artifact-library.test.tsx tests/unit/personal-agents.test.tsx
```

stdin 为空；`Test Files 3 passed (3)`、`Tests 64 passed (64)`、exit 0。

### 最终模块验证

```powershell
.venv\Scripts\python.exe .naumi/transactions/pa02-20261104/ui_run_recorded.py --label ui-module-sealed -- D:/DEV/pnpm/pnpm.exe --dir frontend/web exec vitest run tests/unit/personal-memories.test.tsx tests/unit/personal-items.test.tsx tests/unit/personal-artifact-library.test.tsx tests/unit/personal-artifact-text-materials.test.tsx tests/unit/personal-agents.test.tsx tests/unit/personal-collaborations.test.tsx tests/unit/personal-peer-messages.test.tsx tests/unit/personal-team-turns.test.tsx
```

stdin 为空；`Test Files 8 passed (8)`、`Tests 183 passed (183)`、exit 0。新增记忆测试 29 项；其余 154 项是相关既有回归。不同批次有重叠，不相加冒充唯一测试数。

覆盖中文指纹、白名单持久化、apply503/重载、prepare 响应丢失、GET404、上下文失败恢复、跨 Agent 延迟 GET、错 actor/ref、非法 tombstone、实际表单、来源和冲突 CAS、确切分享/撤销、共享只读、纠正与来源撤销、删除/实际导出、显式 legacy、脏编辑、损坏 token、搜索/冲突分页、12 个真实 HTTP client 路由/认证/no-store。最后自审新增 discard 原 ID/FP、丢响应、并发 apply 获胜、已删除结果恢复、延迟 prepare/apply 不改 URL、旧导出抑制、modal 焦点与 storage 撤权。

### 生产构建

```powershell
.venv\Scripts\python.exe .naumi/transactions/pa02-20261104/ui_run_recorded.py --label ui-build-sealed -- D:/DEV/pnpm/pnpm.exe --dir frontend/web run build
```

stdin 为空；`tsc -b` 完成，Vite `4545 modules transformed`、`✓ built in 1.30s`、exit 0。构建仍有既有 bundle 超过 500 kB 提示，此模块未做无关拆包。

### 自审与保留失败

- `ui-module-first`：4 failed / 78 passed，随后修正共享操作隐藏和受控 fixture 时序，保留原日志。
- `ui-module-second`：82 passed / exit 0，证明当时的 UI 模块，不冒充最后源码结果。
- `ui-module-final`：1 failed / 181 passed，Tab trap 测试暴露 disabled 控件筛选问题；改为控件实际 disabled/tabIndex 过滤后 `ui-module-final-fixed` 为 182 passed。
- `ui-module-sealed` 在增加已提交删除结果提示测试、错 Agent 校验后为 183 passed，是最终源码对应记录。

## 集成与后续验收

本子任务没有启动真实 API/浏览器/模型，没有请求 provider，没有执行生产迁移，没有提交 git。上述是受控 UI/HTTP 测试与生产编译，不代替真实服务端 SQLite、真实 ONNX embedding、真实模型跨工作区或用户浏览器交互验收。父任务统一负责这些验收和源文件副本回滚测试。

已向父任务指出实际 API `State` Literal 需要包含 `conflict`；Service 已实现此过滤。此处不改后端枚举。

原始两份文件保存在 transaction 的 `ui-original/` 中，原 SHA 由 `ui-baseline-manifest.json` 保存；所有 9 个当前文件由 `ui-files.json` 冻结。最终交付前重新打开原件、当前文件和记录，不覆盖原 manifest。

## 2026-11-05 实际浏览器与搜索视觉补充

首次冻结后已完成真实 GUI 新建、纠正、历史、JSON 导出、删除和原 ID 草稿清理，完整证据见 `pa02-personal-memory-ui-acceptance.md`；其中发现的 inactive DTO 空串问题已由 Service 规范为 null，未改变 UI 行为。

随后实际图片复查发现搜索标签继承 column，已仅修改 `memory.css` 的选择器优先级、横向布局和图标防缩，高度从 64px 修正为 40px。当前最终源码对应**完整前端**验证为 `ui-full-frontend-search`：30 files / 311 tests passed / exit 0；生产构建 `ui-build-search`：exit 0。之前的 183 项是首次冻结记录，仍保留但不与 311 相加。

原浏览器只读复查在 1440 与 390 两种尺寸均确认图标和输入中心同 Y、文档宽等于 viewport，无新建/修改/删除请求或 provider 执行。细节、当前图片及保留的修正前 CSS 见 `pa02-memory-search-layout-fix.md`。`ui-files-before-search.json` 保留首次冻结清单，`ui-files.json` 更新当前摘要。
