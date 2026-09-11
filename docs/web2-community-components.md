# Web2 社区组件接入记录

## 2026-09-11：COSS 文件树

- 来源：`cosscom/coss` 的 `apps/origin/registry/default/components/comp-575.tsx` 与 `apps/origin/registry/default/ui/tree.tsx`。
- 许可：COSS 仓库采用混合许可；`apps/origin` 在仓库 `LICENSING.md` 和该目录 README 中明确为 MIT。本项目仅适配这部分，不使用仓库其他 AGPL 目录代码。
- 接入位置：Web2 右侧“文件”页上半区；原有会话附件保留在下半区。
- 底层：新增认证只读接口 `GET /api/v1/workspace/tree`，共享 API client 和类型位于 `frontend/shared`，因此旧 Web 后续也能直接复用，不在 Web2 中重复实现请求逻辑。
- 真实行为：目录树来自当前 daemon 的 `workspace_root`；目录优先、名称排序，支持鼠标与键盘展开、聚焦和选择。
- 安全边界：忽略 `.git`、`.naumi`、虚拟环境、构建产物、缓存和 `node_modules`；忽略符号链接；每个路径再次确认位于工作区内；最多返回 2500 项和 10 层。树保持只读，没有提供会误导用户的拖拽移动。
- 样式：保留参考组件的缩进导引线、无大圆角行、文件类型图标、选中与焦点反馈，使用 Web2 浅色配色。

后续模块：AI Message、Agent Avatar、AI Sources、Border Beam。
