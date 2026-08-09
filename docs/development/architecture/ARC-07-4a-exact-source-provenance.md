# ARC-07.4a Exact Source Provenance

## 目标

给每个 source-free release bundle 和 immutable installed slot 绑定 exact Git commit 与完整 tree listing digest，
使 EVO-05.6b2 能机械证明 rollback slot 对应 Rollback Source 的 baseline，而不是按版本字符串猜测。

这是 ARC-07.4 的最小前置，不等同于签名、notarization、SBOM 或供应链证明已经完成。

## 证据契约

- `source_commit` 必须是 40/64 位小写完整 Git object ID，不接受 branch、tag、短 SHA 或可漂移 ref；
- `source_tree_sha256` 是 `git ls-tree -r -z --full-tree <exact-commit>` 原始 bytes 的 SHA-256；
- artifact assembler 将二者写入 manifest；slot installer 重读、校验并固化到 content-addressed Slot receipt；
- release CLI 未显式传入 provenance 时，从当前 checkout 的 exact `HEAD^{commit}` 机械计算，任一步失败即停止构建；
- 显式参数必须 commit/tree 成对提供，便于受控远端 builder 注入已验证来源。

该 tree digest 算法与 EVO-05.6b1 Rollback Source 完全一致，因此 executor 可以直接进行 digest equality gate。

## 验收结果

- deterministic artifact manifest 保存 exact commit/tree；
- installed slot receipt 继承且校验相同 provenance；
- branch 名等模糊来源在读取任何发行输入前即失败；
- artifact/slot/launcher/installer 相关 29 项小模块测试通过；
- 未运行全量测试。

## 当前边界与下一步

- SHA-256/commit 只形成可复算 provenance；[ARC-07.4b](ARC-07-4b-trusted-build-attestation.md) 已增加
  builder identity 与 detached Ed25519 build attestation，但独立 trust-root 分发、SBOM 与平台 notarization 仍未完成；
- EVO-05.6b2 仍必须要求 current pointer 的 exact previous slot provenance 等于 Rollback Source baseline，并验证
  current pause/request/source 依赖；只凭 provenance 不产生 rollback authority。
