# Model Capability Contract Implementation Plan

1. Write failing config/catalog invariant tests for positive values, output/context, modalities and pricing.
2. Implement strict validation without changing valid existing catalogs.
3. Write Router tests for per-field provenance, verified/partial/unverified/incompatible status and configured max-token compatibility.
4. Implement immutable `ModelCapabilityContract` and expose it through Bridge status.
5. Add Doctor tier aggregation and compact terminal warning rendering.
6. Run only model config/catalog/router, Doctor, Bridge status and terminal contract tests; Ruff/diff check, self-review, independent commit and push.
