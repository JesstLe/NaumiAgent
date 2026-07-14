# 二进制发行操作手册

## 当前边界

- 私有源码仓：`JesstLe/NaumiAgent`；
- 公开产物仓：`JesstLe/NaumiAgent-Releases`；
- workflow：`.github/workflows/release-binaries.yml`；
- 当前只允许 unsigned prerelease；签名接入前禁止把产物标记为 GA。

GitHub 当前标准 hosted runner 标签使用 `ubuntu-24.04`、`ubuntu-24.04-arm`、
`macos-15-intel`、`macos-15` 和 `windows-2025`，分别生成 Linux x64/arm64、macOS
x64/arm64 与 Windows x64。runner 标签发生变化时，以 GitHub 官方 runner reference 为准。

## 一次性配置

1. 创建 public、artifact-only 的 `JesstLe/NaumiAgent-Releases`；
2. 创建只允许该仓 Release contents write 的 fine-grained token；
3. 在私有源码仓设置 Actions secret `DISTRIBUTION_GITHUB_TOKEN`；
4. 可选设置 variable `NAUMI_DISTRIBUTION_REPO` 指向镜像发行仓；
5. 发行仓不要 push 源码分支，只用 GitHub Releases 承载 assets。

## 内部预览发布

```bash
gh workflow run release-binaries.yml -f version=0.1.214
gh run watch
```

五个平台全部成功后，workflow 创建 `v0.1.214` prerelease，并同时上传版本化文件和 latest
稳定别名。预览安装必须显式设置 `NAUMI_VERSION`，因为 GitHub 的 `/releases/latest` 不选择
prerelease。

## GA 前硬门禁

- macOS backend/UI/动态库用 Developer ID 签名；archive 通过 `notarytool`；
- Windows backend/UI 用 Authenticode 签名；
- 两个平台在干净机器通过安装、启动、Bridge 心跳和一次真实模型对话；
- Linux x64/arm64 在 glibc 基线发行版验证；
- 生成第三方许可证清单与 SBOM；
- 将 workflow 的 prerelease 发布步骤替换为仅在签名/公证证据齐全时允许 GA。

## 本地 smoke

```bash
uv sync --extra release
PATH="$PWD/.venv/bin:$PATH" PYTHON="$PWD/.venv/bin/python" \
  OUTPUT_DIR="$PWD/.release-smoke" scripts/release/build_unix.sh
```

这会真实编译 Terminal UI、冻结后端、运行两个入口 smoke、扫描源码泄漏并生成
archive/checksum/manifest。它不是其他平台成功的替代证据。
