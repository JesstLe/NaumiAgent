# NaumiAgent Workbench — 内部签名发布清单

> 适用范围：Mac Workbench 的 Developer ID 签名 + 公证内部发布。公开 App Store 分发不在本清单范围内。

## 1. 前置条件

- [ ] 拥有有效的 **Developer ID Application** 证书（导入到构建机的登录钥匙串）。
- [ ] 拥有 Apple ID + Team ID，并已创建 app-specific password（存入钥匙串 profile）。
- [ ] 本机已安装 Xcode Command Line Tools（`xcrun notarytool`、`xcrun stapler`）。
- [ ] 本机已安装 `uv` / Python 3.13+ 与 `swift` 工具链。

确认身份可用：

```bash
security find-identity -v -p codesigning
# 应能看到 "Developer ID Application: ..."
```

## 2. 发布门（发布前必须全绿）

```bash
apps/macos/NaumiAgentWorkbench/scripts/verify-dev-build.sh
```

该门包含：ruff + 后端单测 + 本地闭环冒烟 + Swift 测试 + 开发打包。任意一项失败不得发布。

## 3. 版本与协议兼容

- [ ] 确定 `NAUMI_BUNDLE_VERSION`（如 `1`、`1.1`、`2.0`），写入发布说明。
- [ ] 确认 app 协议版本与守护进程协议版本兼容（capabilities 的 `protocol_version`）。
  - 守护进程协议升级时，旧 app 必须在连接失败时给出「协议不兼容」提示，而非静默崩溃。
- [ ] 如有不兼容迁移，准备迁移提示文案（中文优先）。

## 4. 签名构建

```bash
cd apps/macos/NaumiAgentWorkbench
NAUMI_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
  NAUMI_BUNDLE_VERSION="1" \
  ./scripts/package-signed-app.sh
```

产物：

- `dist/NaumiAgentWorkbench.app`（已签名，hardened runtime + timestamp）
- `dist/NaumiAgentWorkbench-signed.zip`（供公证上传）

确认：

- [ ] `codesign --verify --strict --verbose=2 dist/NaumiAgentWorkbench.app` 通过。
- [ ] `spctl --assess -vv dist/NaumiAgentWorkbench.app` 不报错（公证后通常会通过）。

## 5. 公证与装订

首次使用前，存储公证凭证（一次性）：

```bash
xcrun notarytool store-credentials "naumi-notary" \
  --apple-id "you@example.com" \
  --team-id "TEAMID" \
  --password
```

提交公证：

```bash
./scripts/notarize-app.sh dist/NaumiAgentWorkbench-signed.zip
```

确认：

- [ ] 公证状态为 `Accepted`。
- [ ] `xcrun stapler validate dist/NaumiAgentWorkbench.app` 通过。
- [ ] `spctl --assess -vv dist/NaumiAgentWorkbench.app` 通过。

## 6. 干净机验证

在一台**没有**安装构建工具的 Mac 上：

- [ ] 双击 `dist/NaumiAgentWorkbench.app` 可启动，无 Gatekeeper 拦截。
- [ ] 启动后连接本地守护进程（`naumi-agent api --host 127.0.0.1 --port 8765`）成功。
- [ ] 运行 Phase B 冒烟流程：创建 mission、创建 issue、claim lease、运行 validation、查看 Dashboard 刷新。
- [ ] 断网（除 localhost 外）时所有页面仍可正常工作。

## 7. 发布物归档

- [ ] 将 `NaumiAgentWorkbench-signed.zip` 归档到内部发布渠道。
- [ ] 记录版本号、签名指纹、公证 submission id、发布日期。
- [ ] 在仓库中打 tag：`git tag mac-workbench-v<version>`。

## 8. 回滚条件

出现以下任意情况，撤销该版本并回滚到上一个已验证版本：

- 干净机无法启动（Gatekeeper 拦截 / 闪退）。
- 连接兼容守护进程时静默失败。
- 真实模式出现 `fixture-` / `design-` 假数据行。
- 任意高风险写操作绕过意图锁或人工审批。
