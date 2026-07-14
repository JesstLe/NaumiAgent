# NaumiAgent 品牌资产治理实施计划

> 对应设计：`docs/superpowers/specs/2026-07-14-brand-asset-governance-design.md`

## 任务 1：确定 canonical 资产

1. 校验选定图、候选图与 AppIcon 的格式、哈希和视觉。
2. 把选定图移动为 `assets/logo.png`。
3. 更新 README 与当前文档引用。

## 任务 2：删除已确认的旧资产

1. 删除 `assets/logo.svg`。
2. 删除重复 variant A 与未采用 variant B/C。
3. 保留 AppIcon、功能截图和 UI 审计图片。

## 任务 3：验证与提交

1. 检查 PNG 尺寸、文件类型和 README 链接。
2. 搜索旧名称与悬空引用。
3. 运行文档治理定向测试和 `git diff --check`。
4. 以英文独立提交。
