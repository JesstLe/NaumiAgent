# NaumiAgent Workbench macOS

SwiftUI shell for the local NaumiAgent Workbench API.

```bash
swift build
./scripts/test.sh
```

### Preview fixture mode（前端快速预览）

Run the macOS app with one of the local fixtures to see populated UI without
connecting to a running daemon:

```bash
cd apps/macos/NaumiAgentWorkbench
./scripts/run-preview.sh zh
./scripts/run-preview.sh en
```

如果传入 `--preview-fixture zh`，默认语言会被设为 `zh-CN`；
如果传入 `--preview-fixture en`，默认语言会被设为 `en-US`。
预览模式不会启动定时刷新（避免覆盖 fixture 状态）。

The test script adds the CommandLineTools framework and runtime paths needed for Swift Testing on machines without the full Xcode app installed.

### Dev app packaging（开发打包）

打包一个本地、未公证的 `.app`，用于内部开发与冒烟测试：

```bash
cd apps/macos/NaumiAgentWorkbench
./scripts/package-dev-app.sh                       # 最小 release app，不含 fixture
./scripts/package-dev-app.sh --include-fixtures    # 包含预览 fixture（离线预览用）
./scripts/package-dev-app.sh --include-fixtures --open   # 打包后立即打开
```

产物：

- `dist/NaumiAgentWorkbench.app` — 可双击启动的 app（默认连接 `http://127.0.0.1:8765`）
- `dist/NaumiAgentWorkbench-dev.zip` — 压缩包，便于分发到其他开发机

签名说明：

- 默认使用 ad-hoc 签名（`codesign -s -`），仅在本机可直接启动。
- 如需 Developer ID 签名，设置环境变量 `NAUMI_SIGNING_IDENTITY="Developer ID Application: ..."`。
- 该脚本**不执行公证**（notarization）。面向外部分发的签名构建见 `scripts/package-signed-app.sh`。

> `dist/` 已被 `.gitignore` 忽略，产物不会进入版本控制。

