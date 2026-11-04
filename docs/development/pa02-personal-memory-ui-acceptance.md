# PA-02 实际浏览器验收

## 环境与结果

本验收在 2026-11-05（Asia/Shanghai）使用本机已安装 Chromium 1217、真实生产构建前端、真实 API、独立 Personal catalog / session / memory SQLite 完成。没有 mock Service/Store，没有伪造 HTTP 响应，没有 provider 执行，没有修改冻结的 9 个 UI 文件。

- Origin：`http://127.0.0.1:18772`
- Workspace：`.naumi/transactions/pa02-20261104/live-data/A/workspace`
- Echo：实际动态创建的 `pa_6f10118dc20b427d942a1b03ff50c089`，version 1。
- 验收 run：`857069967630`。
- 最终记录：`.naumi/transactions/pa02-20261104/ui-browser-resume-dto-record.json`。
- 结果：**exit 0**。

```text
PASS isolated_real_server_and_dynamic_agent
PASS actual_memory_navigation
PASS reconciled_completed_original
PASS delete_tombstone_body_free_export
PASS original_prepared_draft_discarded
PA02_UI_ACCEPTANCE=PASS; ORIGINAL_REQUESTS_ONLY=PASS; PROVIDER_EXECUTION=0
RECORDED_EXIT=0
```

## 实际用户链路

1. 从真实主导航进入记忆，选择主 Agent。
2. GUI 手动新建 personal 记忆，恢复同一原请求并查看实际列表与详情。
3. GUI 纠正；原 topic/category/scope 锁定，操作携带实际 revision/SHA。
4. 查看真实修订记录；通过 GUI 下载真实 JSON，纠正后正文存在、旧正文不再导出。
5. 检查浅色桌面及 390px 详情/列表。实际文档宽 390、viewport 390，无横向溢出。
6. GUI 删除；后端公共 DTO 对齐后，GUI 原样核对同一已提交删除请求，清除 pending，不重复删除。
7. 删除后实际 JSON 导出不含旧正文或纠正正文；详情仅显示已删除状态。
8. GUI 发出独立草稿 prepare；脚本实际转发后丢弃真实响应，浏览器保留原 opaque token。该故障是显式网络响应丢失，不是假响应。
9. 用户二次确认「清理草稿」，GUI GET → 同 ID discard → GET，得到 erased/result=null；查询确认没有创建该草稿记忆。
10. localStorage 检查无本轮 topic/content，记忆 token 严格白名单；最终 pending 数为 0，reload 后新建按钮可用。

## 原请求身份与最终只读复核

| 阶段 | 原 request ID | 最终 GET |
| --- | --- | --- |
| 创建 | `393d5926717c47b9b8d13326dc7d7857` | applied；当前 rev3 deleted，正文/来源 null |
| 纠正 | `d1714e53608a4cf3a5dfba3528c4215b` | applied；同一当前 tombstone，不返回旧正文 |
| 删除 | `037e1f3dbb224c8689f46d1d503b9fab` | applied；同一当前 tombstone |
| 未提交草稿/清理 | `fcdeb5d0f9394757a93f2c38fdd6d32d` | erased；result=null |

前三个请求共同指向 `pm_c6b6005c2c302d433db85cfcfb0356f1`，最终 revision 3。草稿 prepare/discard 始终使用同一第四个 ID。重载没有重建主题或更换 mutation 身份。

## 真实集成发现及修复

前 3 个 attempt 暴露了验收脚本的异步时序问题：prepare 转发后过早 GET404、恢复操作完成后重复等待同一按钮、请求擦除与最终状态之间的短暂可见性。脚本仅等待/恢复原 ID，从未把 404 当未受理或新造请求。原始失败输出和截图全部保留。

第 4 个 attempt 发现真正的 UI/API 集成缺陷：Store 删除后内部正文是空串，但公共 DTO 原样返回 `content=''`，冻结 UI 的 tombstone 校验遵循合同要求 `content=null`，因此已提交删除仍保留 pending。Root 在 Service `_dto` 将 deleted/rejected/superseded 公共 content/source 规范为 null，并标记 source_available=false；Store 内部语义不变，UI 源码未改。Root 重启独立 server 后，原删除 GET 恢复成功，第 5 个 attempt 通过。

## 截图与导出

以下实际图片均已重新打开并目视检查，所有保存的截图 SHA 均已复核：

- `.naumi/transactions/pa02-20261104/ui-acceptance-desktop-1440-attempt04.png`
- `.naumi/transactions/pa02-20261104/ui-acceptance-detail-390-attempt04.png`
- `.naumi/transactions/pa02-20261104/ui-acceptance-list-390-attempt04.png`
- `.naumi/transactions/pa02-20261104/ui-acceptance-final-1440-attempt05.png`

实际 GUI 下载并重新读取：

- `ui-acceptance-export-before-delete.json`：2842 bytes，SHA `4e9b62de3094a69b5fd73c06602419664eb1a37975ac65bb07a409c17b95e6fd`。
- `ui-acceptance-export-after-delete.json`：182 bytes，SHA `c583ce50184dcce2bffa6777719836d1de765baa89f3d74b35e21e6a561d8f64`。

独立结构化摘要为 `ui-browser-verification.json`；执行与恢复细节保存在 `ui-acceptance-checkpoint.json` 和 append-only attempts 的 `ui-acceptance-report.json`。

本轮实际浏览器只验收上述窄链路。候选冲突、分享撤权、实际 embedding 与模型跨工作区行为使用父任务的独立验收证据，不把受控单测或此浏览器窄链路扩称这些完整能力已验证。
