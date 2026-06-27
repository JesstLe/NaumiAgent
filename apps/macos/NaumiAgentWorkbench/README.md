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
