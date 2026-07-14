# Naumi project configuration layout design

## Objective

Move NaumiAgent's default project configuration from a root-level `config.yaml` to
`.naumi/config.yaml` without breaking existing projects. Keep model credentials out of both files
and make every local runtime surface resolve the same configuration path.

## Decision

NaumiAgent will use a project-local `.naumi/config.yaml` as its default configuration. This is a
better fit than a root `config.yaml` because the configuration controls project-specific workspace,
permission, provider, UI, session, and tool behavior. It also causes existing config-relative
runtime defaults such as `data/sessions.db` and `data/chroma` to live under `.naumi/data/`.

This slice does not introduce layered global configuration. A future `~/.naumi/config.yaml` may
hold user profiles, but merging global and project files now would make relative-path ownership and
safety overrides ambiguous. Secrets remain in the OS credential store or environment variables.

## Alternatives considered

1. Keep `config.yaml` at the project root. This has the smallest diff but continues to expose
   Naumi-specific state at the top level and anchors runtime data outside `.naumi/`.
2. Use `.naumi/config.yaml`. This keeps project state together and supports deterministic
   config-relative paths. This is the selected approach.
3. Layer `~/.naumi/config.yaml` with project `.naumi/config.yaml`. This is powerful but requires a
   separate merge and provenance design, especially for relative paths and safety policy.

## Resolution contract

One shared resolver in `naumi_agent.config.paths` owns all defaults and compatibility behavior.

When the caller uses the default `.naumi/config.yaml`:

1. Starting at the process working directory, inspect it and each parent for
   `.naumi/config.yaml`. The nearest match wins.
2. If no modern config exists in any ancestor, repeat the walk for legacy `config.yaml`. The
   nearest legacy match wins.
3. If neither exists, return `<cwd>/.naumi/config.yaml`. Onboarding may create this file and its
   parent directory.

The modern search completes before the legacy search. Therefore a parent project modern config
wins over a nearer legacy file and prevents a stale compatibility file from shadowing the current
source of truth.

When a caller supplies any non-default `--config` value, that path is authoritative. Existing paths
are returned, and missing paths remain missing so the relevant command can create or report that
exact path. The resolver never replaces an explicit missing path with an unrelated repository file
or example configuration.

Paths containing `~` are expanded. The resolver returns an absolute selected file path;
`AppConfig.from_yaml()` still anchors paths inside that YAML to the selected file's parent.

## Surface integration

The following entry points use `DEFAULT_CONFIG_PATH` and the shared resolver:

- default CLI, classic CLI, legacy TUI, new terminal UI launcher, task runner, server, doctor, and
  configure commands;
- Python JSONL bridge;
- direct Node terminal UI argument parsing;
- deployment validation when no explicit config is supplied;
- Windows setup and README setup commands.

Container entrypoints continue to pass `/app/config.yaml` explicitly. Explicit paths are outside the
default migration contract and therefore remain unchanged.

The tracked root `config.yaml.example` remains the distribution template for compatibility. Its
instructions create `.naumi/config.yaml`, and its provider catalog example becomes `providers.json`
because model catalog paths are anchored to the configuration directory.

## Migration and persistence

Existing root `config.yaml` files are read in place through the legacy fallback. NaumiAgent does not
copy, move, rewrite, or delete them automatically. Automatic copying could create two competing
configurations and could duplicate plaintext legacy credentials before credential migration runs.

New onboarding and Windows setup create `.naumi/config.yaml` when neither modern nor legacy config
exists. Windows setup leaves an existing legacy root config active instead of creating a competing
modern file. Git ignores the local config, `.naumi/providers.json`, and `.naumi/data/`, while tracked
reusable `.naumi/skills/` remain visible.

## User experience and errors

Runtime diagnostics continue to show the resolved configuration path, so users can see whether a
modern, legacy, or explicit file is active. A missing explicit path is never silently hidden by the
example file. Commands that require an existing config report the requested path; onboarding-capable
commands create it after the existing prompts and credential checks succeed.

All new user-facing setup text names `.naumi/config.yaml` in Chinese. Model API keys are described as
Keychain/credential-store or environment data, never project YAML data.

## Verification

Targeted tests cover:

- nearest modern ancestor discovery;
- modern-over-legacy precedence;
- nearest legacy fallback;
- missing default creation target;
- explicit existing and missing paths without fallback;
- `~` expansion and Windows-compatible path construction;
- CLI, bridge, deployment, and Node defaults;
- onboarding persistence into a missing `.naumi/` directory;
- config-relative session, vector, and provider catalog paths under `.naumi/`;
- a real temporary-project launch/config load scenario.

Only the configuration, onboarding, terminal launcher, bridge resolver, deployment, and Node
protocol test modules are run for this feature. The repository-wide suite is intentionally excluded.

## Out of scope

- Global `~/.naumi/config.yaml` layering or profile selection.
- Automatic deletion or relocation of legacy root configuration.
- Model reasoning-effort controls; those are the next independent feature.
- Changes to explicit Docker `/app/config.yaml` mounts.
