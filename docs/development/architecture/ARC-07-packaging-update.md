# ARC-07 跨平台闭源打包与安全更新

## 目标

用户安装获得签名后的运行产物而非完整源码，同时保留许可证义务、插件扩展、离线安装、诊断、
升级和回滚能力。

## 子模块

- ARC-07.1 Artifact layout：launcher、Python runtime、Node frontend、assets、schemas、licenses。
- ARC-07.2 Build matrix：macOS arm64/x64、Linux x64/arm64、Windows x64。
- ARC-07.3 Source exposure audit：wheel/sdist/cache/debug/source map 中的源码和 secret。
- ARC-07.4 Signing/notarization：macOS codesign/notary、Windows signing、checksums/SBOM。
- ARC-07.5 Updater：channel、manifest、signature、download、atomic switch、rollback。
- ARC-07.6 Config/data compatibility：用户 `.naumi` 与 state 不被覆盖，迁移前备份。
- ARC-07.7 Offline/enterprise：离线包、代理、镜像、禁用自动更新。
- ARC-07.8 Crash/diagnostic symbols：保护源码与可诊断性的平衡。

## 验收标准

- 从发布 URL/包管理器安装不下载项目 Git 仓库和测试源码。
- 解包审计只包含允许的运行字节码/资源/license/schema；无 API key 和开发路径。
- 签名/校验失败拒绝更新；中途断电保持旧版本可启动。
- 更新后首次启动迁移失败自动进入安全模式并可回滚二进制与数据快照。
- 三平台 clean install、upgrade N-1、rollback、offline、代理环境通过。
- SBOM、第三方许可证、版本和 build provenance 可查询。
