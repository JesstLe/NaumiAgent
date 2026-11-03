# 品牌资产：黑底白 N

## 定稿

2026-11-03 按用户提供的黑色圆角底、白色斜体 N 参考图重新绘制。
保留参考图的炭黑 `#181B23` 与白色 `#FFFFFF`，不采用前轮青色方案。
原图为 43×38 像素截图，交付使用矢量重建，而非放大截图。

- 唯一图形母版：`assets/logo.svg`，128×128 坐标系，无字体、网络资源或脚本依赖。
- README / 文档：`assets/logo.png`，1024×1024 RGBA，圆角之外透明。
- Web2 欢迎区和侧栏、旧版 Web 导航：直接导入母版；Vite 管理打包与缓存。
- 浏览器：SVG favicon、七尺寸 ICO、180×180 Apple Touch Icon。
- Windows：原有 14 个 PNG 与 ICO / ICNS 打包路径不变。
- macOS：AppIcon.icns 同步更新；运行时 SwiftUI 图形从 SVG 编译生成。
  不增加 Bundle.module、资源包定位或应用包装依赖。
- 终端 ASCII 品牌与历史功能截图保留，不把历史截图当作当前品牌资源替换。

## 生成

```powershell
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend exec playwright install chromium
.venv/Scripts/python.exe scripts/generate_brand_assets.py
```

本机已有 Chromium 时，可通过 `BRAND_CHROMIUM_PATH` 显式指定其可执行文件。
生成器先在临时目录完成所有产物，渲染或解析失败时不发布半成品。
`assets/brand-manifest.json` 记录母版及派生资产 SHA-256；禁止手工修改派生资源。
`.gitattributes` 固定被哈希文本的 LF 换行，避免 Windows checkout 改变资产身份。
变更母版后，应重新生成并更新测试中的已确认母版哈希，防止意外设计漂移。

## 验证与自审

- 品牌测试覆盖母版身份、PNG 像素与透明度、全部派生哈希、14 个尺寸、ICO 多帧、
  ICNS 解码、macOS 原生路径一致性、非法几何拒绝以及 Web 引用。
- 完整前端单测和生产构建用于检查实际 import / 打包链。
- 本轮在 Windows 上检查 macOS 图标容器与生成源码；macOS 原生编译和安装显示另行验收。
- 资产修改与已安装应用更新是两个阶段：当前修改仓库资产，不代表已经重装或发布桌面应用。

最终命令、输出、差异与复制回滚记录见本次本地事务目录
`.naumi/transactions/logo-final-20261103/`。

### 本轮结果

- `ruff check src/ scripts/generate_brand_assets.py tests/unit/test_brand_assets.py`：通过。
- Python 包 import：通过；品牌资产测试 43 项通过。
- 前端单元测试：17 个文件、104 项通过；生产构建通过。
- 专项 Playwright：1 项通过，实际浏览器解码双入口图标与 favicon，并检查三种宽度；
  后端使用仓库已有 mock，不声称已连接真实业务后端。
- 重新生成 22 个派生产物：全部字节一致。
- 在另一份文件副本回滚：恢复 36 个文件，旧版品牌测试 4 项通过。
  对之后被修改的文件进行回滚尝试：哈希保护拒绝，写入数为 0。
- 全量 `pytest tests/ -x -q` 在
  `test_real_manager_bridge_node_stop_and_textual_parity` 的异步事件等待处超时，
  输出 `1 failed, 4 warnings in 340.10s`，清理线程未退出后停止本轮测试进程。
  本次只修改品牌模块，未修复该测试；全量 Python 回归仍未通过。
