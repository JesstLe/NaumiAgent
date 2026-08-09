# ARC-07.4b Trusted Builder Identity and Build Attestation

## 目标

为每个 source-free release archive 生成分离式 Ed25519 构建证明，使后续安装、候选准入和更新链能够机械确认：

- archive 与 manifest 未被替换；
- manifest 声明的 exact source commit/tree、版本和目标平台由受信 builder key 签署；
- 构建来自明确的 repository、workflow ref、run id、run attempt 和带时区构建时间；
- 私钥没有写入 bundle、archive、checksum 或 attestation。

本切片建立 builder identity 与 build provenance，不等同于 macOS notarization、Windows Authenticode、Linux
包签名、SBOM 或最终用户 trust root 分发已经完成。

## 证明契约

`ReleaseBuildAttestationPayload` 使用固定 domain `naumi.release.build-attestation.v1`，签名覆盖：

1. product、version、target；
2. exact `source_commit` 与 `source_tree_sha256`；
3. manifest SHA-256；
4. versioned archive logical name 与 archive SHA-256；
5. builder id、key id、key generation、公钥及公钥摘要；
6. repository、workflow ref、run id、run attempt、built_at。

签名采用 canonical UTF-8 JSON 和 Ed25519。Attestation 自身以及 Build Trust Policy 都是 content-addressed exact
artifact；字段增加、删除、重复 key identity、非 canonical Base64、错误摘要和无时区时间全部失败关闭。

## Builder Trust Policy

`ReleaseBuildTrustPolicyDocument` 保存公开信任材料，不保存私钥。每个 key 明确声明：

- stable builder/key identity 与 generation；
- Ed25519 public key 和 SHA-256；
- `active` 或 `revoked` 状态；
- `valid_from`、可选 `valid_until` 与撤销时间。

验证要求 attestation 的 exact identity 存在于当前策略、key 仍 active，且构建时间落在 key 有效窗口内。旧 generation
不会因为同一 builder 的新 key 存在而自动获得信任；撤销 key 即使签名数学上有效也会被拒绝。

Trust Policy 的可信分发不能依赖同一个待验证 archive。正式 installer/updater 必须从 launcher 内置 pin、平台签名安装器
或管理员控制的独立通道获得 policy/fingerprint；从下载目录随包读取任意 policy 不构成信任。

## 发行链接线

- `assemble_release_artifact()` 在提供 `ReleaseBuildSigner + ReleaseBuildContext` 时，在 archive 完成后生成
  `<archive>.attestation.json`；证明与 archive 分离，避免 manifest 自签名循环；
- `scripts/release/assemble_artifact.py` 默认要求环境中的 builder 私钥和身份字段；只有显式
  `--allow-unsigned-development-artifact` 才能构造本地开发夹具；
- GitHub release matrix 要求 `NAUMI_RELEASE_BUILDER_PRIVATE_KEY_BASE64` secret 以及 builder id/key id/generation
  repository variables；私钥只注入 platform build step，脚本在运行 Bun/PyInstaller 前移除环境变量，仅向最终签名
  Python 进程短暂恢复，然后清空 shell 变量；
- stable alias 可以改变 transport filename，但 archive bytes 必须仍匹配 payload 中签署的 versioned archive digest；
- 发布说明不再把产物描述为 unsigned，但仍明确 GA 被平台签名/notarization 与 trust-root distribution 阻断。

## 验收结果

- 真实 Ed25519 seed 生成 deterministic signature，并由 active trust policy 验证；
- archive、manifest、source provenance、builder identity 和 CI run identity 均被同一 payload 绑定；
- unknown、revoked、尚未生效的 key 全部失败关闭；
- manifest 篡改、archive 篡改和结构合法但密码学错误的 signature 全部被拒绝；
- 输出中不存在 private key Base64；
- release workflow 缺少签名 secret 时不能生成正式产物；
- Build Attestation 与 Release Artifact 两个小模块共 14 项测试通过，包含真实 release CLI，未运行全量测试。

## 当前边界与下一步

- 本切片不在仓库中保存生产私钥，也不生成生产 key；仓库 secret/variable 的实际配置仍是外部发布运维门槛；
- Trust Policy 尚未由平台 installer/launcher 以独立可信根分发，因此不能宣称任意下载端已具备 production trust；
- EVO-05.5e 下一步必须要求调用方同时提供 detached attestation 与受信 policy，先验证证明再安装 candidate，并把
  attestation/policy digest 固化到 Candidate Admission；仅凭 manifest commit/tree 不再产生 activation input authority；
- ARC-07.4c 仍需 SBOM 与 dependency provenance；ARC-07.4d 仍需 macOS/Windows 平台签名和 notarization。
