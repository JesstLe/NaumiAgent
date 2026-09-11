# Web2：Codex 风格工作区

## 目标与入口

用户参考图：2026-09-11 提供的 Codex Windows 工作区。新增 `/web2`，复用 `frontend/web` 的 React、Vite、Lucide SVG 和现有 FastAPI 服务。旧入口保持可用。

## 同级目录调整（2026-09-11）

按用户要求，将新版从 `frontend/web/src/web2` 移至 `frontend/web2/src`；公共 API、控制器、Provider、平台适配和会话状态移至 `frontend/shared/src`，对应测试移至 `frontend/shared/tests`。两个界面均通过 `@naumi/shared` 消费公共能力，`web2` 不引用旧版源码。

Web 依赖合并为 `frontend` 下的 pnpm workspace（web/web2/shared），锁文件统一移至 `frontend/pnpm-lock.yaml`。统一应用装配入口和构建产物仍在 `web`，保持 `/chat`、`/web2`、同路由 Provider 生命周期及 Windows 打包脚本兼容。

迁移后验证：根目录与原 `web` 目录冻结锁文件安装均通过；TypeScript + Vite 构建、27 项单元测试、13 项 Playwright 测试通过，覆盖跨界面草稿与流状态保持。Ruff 与 API import 通过。重启本任务 5174 开发服务后，用 Chrome 访问真实后端的 `/web2`，历史会话和模型列表正常加载、页面无运行时异常，并检查截图。公共包未引用任一界面源码，构建产物仍为 `web/dist`。本次目录迁移未重复调用模型，也未重新构建 Windows 安装包；此前全量 Python 套件的未通过状态保持如实记录。

布局：240px 渐变项目侧栏、白色双栏工作区、底部输入框、可收起的执行记录面板。避免营销文案，数据来自本地服务，不填充演示项目。

## 实现范围

- 新建、切换、搜索历史会话；草稿及布局偏好本地持久化。
- SSE 流式对话、附件、模型选择、计划模式、权限审批、停止执行。
- 真实 Git 差异、来源文件、工具清单、任务状态、执行记录。
- 连接失败保留界面，支持配置 API 地址／令牌后重连。
- 浏览器入口使用经过协议校验的外部链接；网页中不伪装原生浏览器或交互式终端。

## 当前进度

已实现并接入真实服务。新界面在 `/web2`，旧界面在 `/chat`。两个入口可在各自设置／侧栏中互相切换。

## 解耦约定

```text
App -> PlatformProvider -> WorkspaceProvider
                               |
                      useWorkspaceController
                               |
                     WorkbenchRuntimeClient
                               |
                       WorkbenchApiClient
                               |
                         FastAPI / 引擎
                               |
              Web2 视图       原 Web 视图
```

- `WorkspaceProvider` 位于路由上方；路由切换不重新创建控制器、不清空草稿、不重新启动运行。
- `useWorkspaceController` 是唯一会话生命周期实现，负责发送、SSE、取消、附件、审批、模型切换、分页、错误恢复及实时快照。新增底层能力必须在此处／API client 实现，视图仅调用 action。
- `useWorkbenchConnection` 是旧领域页面的兼容适配器；旧 `sessionStore` 只接收共享状态投影。原版 ChatPage 已删除独立发送与加载逻辑，直接消费共享工作区。
- UI 展开、分栏、搜索词等属于视图状态。草稿、会话选择和后端连接属于共享状态，浏览器存储键使用 `naumi:workspace:*`。
- `WorkbenchApiClient` 修复会话更新／删除误用集合路径的问题，统一使用 `/sessions/{id}`；HTTP 操作设置有限超时。SSE 单独使用可取消的流请求。
- 防止重复提交，快速切换忽略旧响应，IME 输入回车不误发，失败保留草稿；流状态以服务端事件为准，不把断流当作完成。

## 自我审视与明确边界

- 参考图的浅色渐变、单侧栏、双栏、灰色项目条、底部输入框、执行面板均已实现，品牌保留 NaumiAgent，Logo 是 SVG。
- 浏览器入口打开 HTTP(S) 新标签页；原生嵌入浏览器、网页 PTY 终端没有现成后端接口，本次不伪造这些能力。执行面板显示真实 SSE 工具输出／持久运行记录。
- 文件面板展示当前会话来源文件，不声称为任意本地文件系统浏览器。
- 工具清单来自服务端配置；任务来自工作区快照。没有将不存在的定时任务／Pull Request 服务伪装为可用。
- 新界面消息支持纯文本和代码围栏；尚非完整富文本 Markdown 编辑器。
- 实测目标为浏览器版本；未重打包 Windows Tauri 安装包。

## 验证记录（2026-09-11）

- TypeScript + Vite 生产构建通过。
- 前端 27 项测试通过，覆盖中文 SSE 字节边界、空流／坏事件、URL 校验、共享草稿／模型／回复、快速切换、重复发送、失败保留草稿、附件选择、跨视图审批／取消；仅响应边界不算服务端终态。
- Playwright 13 项通过：原版页面导航、新旧 Web 切换、真实契约 mock SSE、Git 差异、草稿刷新、搜索、断网恢复、390/768/1280/1920 响应式；这些不计作真实模型验收。
- `ruff check src/` 通过，API import 通过。
- API、认证、中间件、Workbench、ChatRun、环境、streaming、Workbench API 集成共 435 项 pytest 通过（15.77 秒），包括真实文件／SQLite 上传回归。
- `pytest tests/ -x` 全量收集 6256 项，首次出现失败后没有正常退出；带诊断重跑仍未完成，已停止该测试进程。没有把全量套件标记为通过，也未修改无关 Python 代码来掩盖问题。
- 真实后端使用已有 `.naumi/config.yaml`；浏览器发送“请仅回复 Web2 共享引擎已连接，不要执行工具”，收到实际 `kimi-k3` 回复和持久化已完成运行记录。原版 Web 打开同一会话，实际回复可见。
- 原版 Web 继续发送“请只回复原版 Web 共用成功，不要执行工具”，收到实际回复，切回 Web2 显示相同会话和两次完成记录，验证双向共享。
- 真实附件上传发现并修复两处问题：前端必须在 await 前复制会变动的 FileList；后端来源记录没有 run_id，上传响应改用协议默认空值。修复后真实上传 note.txt 返回成功，输入框附件和文件面板选择状态一致。上传目录已加入 Git 忽略项。
- 真实代码审查读取当前 `main` 分支和实际工作区文件差异。未触碰用户原有 `src/naumi_agent/ui/doctor.py`、`.naumi/terminal-ui-debug.jsonl` 更改。

## 启动

后端在仓库根目录设置 `NAUMI_CONFIG=.naumi/config.yaml` 后运行 `.venv/Scripts/python.exe -m uvicorn naumi_agent.api.app:app --host 127.0.0.1 --port 8765`。

前端在 `frontend/web` 运行 `npm run dev -- --host 127.0.0.1 --port 5174 --strictPort`，打开 `http://127.0.0.1:5174/web2`。现有 5173 服务保持不动。默认通过 Vite 同源代理访问后端，也可在设置中指定 API 地址。
