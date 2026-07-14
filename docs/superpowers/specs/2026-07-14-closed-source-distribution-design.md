# 闭源二进制分发设计

## 现状与目标

源码仓 `JesstLe/NaumiAgent` 已是 private，但旧安装器仍执行 `git clone`，且仓库没有
Release。目标是让普通用户只下载平台产物，不需要 Git、Python、Node 或源码访问权限。

“闭源”在此严格定义为：发行包不包含 Naumi 的 Python/JavaScript 源文件、测试、设计文档、
Git 元数据或构建清单。任何本地二进制仍可能被逆向，因此不作“绝对不可逆”的虚假承诺。

## 发布边界

```text
private source repo
  -> per-platform trusted runner
  -> compiled Python backend + compiled Terminal UI
  -> source-leak gate + checksums + manifest
  -> public artifact-only distribution repo
  -> checksum-verifying installer
```

- 源码仓只负责 CI 构建，不向用户发放 read 权限。
- 发行仓建议为 `JesstLe/NaumiAgent-Releases`，只包含 Release assets，不镜像源码历史。
- Python 后端使用 PyInstaller onedir 冻结；Terminal UI 使用 Bun compile，用户不再依赖 Node。
- 冻结后端通过内部 `__ui-bridge` 入口启动 Bridge；该入口不改变开发态公共 CLI。
- 开发态仍保留源码运行、Node UI 和 Textual fallback。

## 产物

每个平台产物包含：

- `naumi` / `naumi.exe`；
- `naumi-ui` / `naumi-ui.exe`；
- PyInstaller 运行库与第三方依赖；
- `config.yaml.example`；
- `manifest.json`（版本、目标平台、全部文件 SHA-256）；
- archive 的独立 `.sha256`。

不包含 Naumi 自有 `.py`、`.pyc`、`.js`、`.ts`、source map、tests、docs、`.git`、
`pyproject.toml` 或 `package.json`。第三方运行库如必须携带数据文件，记录在 manifest 中并受
依赖许可证约束。

## 安装与升级

- macOS/Linux 安装器识别 OS/arch，下载 archive 与 checksum，校验后原子安装到版本目录，
  再更新 `~/.local/bin/naumi` 软链。
- Windows PowerShell 安装器执行等价的 TLS 下载、SHA-256 校验与版本目录切换。
- 安装器默认从发行仓 latest Release 下载；固定版本和私有镜像可通过环境变量覆盖。
- 安装失败不能破坏当前可用版本。

## 发布安全

- GitHub workflow 仅由 tag 或显式 dispatch 触发；PR/push 不发布。
- 发布到发行仓使用最小权限 fine-grained token，仅允许写该发行仓 Releases。
- 每个 runner 只上传最终 staging/archive，不上传 checkout 或构建缓存。
- 发布 job 在所有平台构建、源码泄漏门禁和 checksum 通过后才创建 Release。
- macOS/Windows 代码签名和 Apple notarization 是公开 GA 的硬门禁；在证书接入前只允许
  `prerelease` 内测产物，不把未签名包描述为正式可信发行。

## 非目标

- 本切片不改变仓库 visibility（它已经是 private）。
- 不自动创建或公开发行仓，不替用户生成 PAT/签名证书。
- 不把混淆或冻结宣传为密码学保密。
- 不在这一切片捆绑 Playwright Chromium；运行时优先系统 Chrome/Edge，后续独立评估浏览器包。

## 验证

- launcher 单测覆盖编译 UI 和冻结 Bridge 命令；
- assembler 单测覆盖原子 staging、manifest、文件 hash、源码泄漏与符号链接逃逸；
- installer 静态/沙箱测试覆盖平台映射、checksum、失败不覆盖旧版本；
- 本机真实编译 Terminal UI 并运行协议读取 smoke；
- PyInstaller spec 语法/收集 smoke；完整四平台产物以 GitHub matrix 为最终证据。
