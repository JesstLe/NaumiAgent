# UI-11 全屏任务与 Timeline 导航

## 目标

将当前嵌入式 `/tasks` 面板升级为可聚焦的全屏列表/详情双栏，同时保持窄屏单页降级。

## 子模块

- UI-11.1 View model：source/status/owner/dependency/priority/age 统一字段。
- UI-11.2 List navigation：上下、分页、首尾、搜索、过滤、稳定 selection id。
- UI-11.3 Detail navigation：事件流、日志引用、artifact、children、依赖图。
- UI-11.4 Live update：选中项消失、排序变化、增量事件和 pinned refresh。
- UI-11.5 Actions：cancel/retry/open artifact/takeover，按来源限制能力。
- UI-11.6 Accessibility：无颜色状态、键位帮助、屏幕阅读友好文本。

## 验收标准

- 10k timeline events 只渲染 viewport，输入和滚动 P95 小于 100ms。
- 过滤刷新后 selection 仍指向相同任务；任务消失时选择最近邻并提示。
- cancel 1s 内显示 pending，终态由后端确认，不能前端乐观伪造。
- background/browser/subagent/todo 的不可用动作不展示或明确禁用原因。
- 触摸板滚动有平滑限速，键盘导航逐项精确。
- 真实多来源任务运行时完成 resize、filter、detail、cancel、resume E2E。

## 实现进度

- `UI-11.2a`（2026-07-17）已完成：现有嵌入式任务面板支持方向键逐项导航、
  `PageUp/PageDown` 有界翻页、`Home/End` 首尾定位和 `/tasks search <关键词>` 本地搜索。
- 搜索覆盖任务行的 ID、标题和 owner/cwd 等详情字段，不修改 Bridge 协议或后端权威数据；
  `/tasks search clear` 恢复完整列表。
- 后端刷新或本地筛选后优先按稳定任务 ID 保持选择；原任务消失时选择原位置最近邻，
  并明确提示用户，不伪造任务终态。
- 10,000 条任务事件的组件真实渲染只输出 viewport 行数，12 次采样 P95 受 100ms
  回归门约束；真实终端进程已验证方向键和首尾键序列。

仍未完成：`UI-11.1` 完整统一字段、全屏双栏/窄屏降级、详情依赖图、retry/artifact/
takeover 能力矩阵，以及屏幕阅读器语义。`UI-11` 因此保持 `partial`。
