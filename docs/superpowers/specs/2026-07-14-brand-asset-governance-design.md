# NaumiAgent 品牌资产治理设计

日期：2026-07-14

## 1. 目标

仓库只保留一个可直接用于 README、文档和发布物的 canonical NaumiAgent logo，避免旧版深色 N 标志、设计候选和重复文件继续混用。

## 2. 审计结论

- `assets/logo.svg` 是旧版深色六边形 N 标志，已过时；
- `logo-variant-a-minimal.png` 与最终选定图 SHA-256 完全一致，是重复副本；
- variant B/C 是未采用的设计候选；
- `naumiagent-workbench-logo-selected.png` 是当前选定图；
- macOS `AppIcon.icns` 渲染后与当前选定图一致，是平台打包资产，不是过时副本。

## 3. 文件规则

- canonical 源：`assets/logo.png`；
- README 和当前文档只引用 canonical 源；
- macOS 发布资产：`apps/macos/NaumiAgentWorkbench/Resources/AppIcon.icns`，继续保留；
- 删除旧 SVG、重复 A 和未采用 B/C；
- 功能截图、界面审计图和终端 ASCII 标识不是 logo 候选，不删除；
- 后续更换 logo 必须原子更新 canonical 源、平台图标与引用，不再以 variant 文件名长期留存候选。

## 4. 验证

- 校验 `assets/logo.png` 为 1254×1254 PNG；
- 视觉核对 canonical 与 AppIcon 一致；
- `rg` 确认没有旧文件名引用；
- 文档链接与治理检查通过；
- Git 中不存在其他 logo variant 文件。

## 5. 自我审视

- AppIcon 是相同品牌图的多尺寸打包产物，不能为了“去重”删除，否则 macOS 应用失去正式图标。
- 历史实施计划中的旧路径只做链接迁移，不改变当时决策结论。
- 本轮不重新压缩 PNG，避免无依据改变最终选定图的色彩与边缘质量。
