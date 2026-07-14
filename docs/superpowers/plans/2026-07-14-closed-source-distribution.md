# Closed-source Distribution Implementation Plan

1. Add failing launcher tests for a compiled UI companion and frozen internal Bridge dispatch.
2. Implement source/development and frozen/release launch paths without changing the public CLI.
3. Add a deterministic artifact assembler and failing tests for manifests, hashes, source leaks,
   symlink escapes and replacement safety.
4. Add PyInstaller/Bun build scripts and build the Terminal UI locally as a real smoke scenario.
5. Replace the public installation path with checksum-verifying macOS/Linux and Windows installers;
   keep the old source installer only as an explicitly named developer installer.
6. Add a private-source-to-artifact-repository GitHub Release matrix with prerelease/signing gates.
7. Update README and release docs, run only packaging/launcher/installer/docs checks, self-review,
   commit and push this slice to main.
