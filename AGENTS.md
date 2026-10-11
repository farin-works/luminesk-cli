# Repository instructions for coding agents

This file applies to the entire repository. A more deeply nested `AGENTS.md`
may add or override instructions for its own subtree.

## Operating contract

- Work only on the task the user explicitly requested. Make the smallest
  coherent change that solves it.
- Read the relevant implementation, tests, documentation, and configuration
  before editing. Do not infer a contract from a filename alone.
- Preserve unrelated user changes. Before editing, inspect the working tree;
  before finishing, inspect the complete diff.
- If requirements are ambiguous and different interpretations would change a
  public contract, security boundary, data format, or destructive behavior,
  stop and ask.
- Do not perform adjacent cleanup, speculative hardening, broad refactors,
  dependency upgrades, or documentation rewrites unless they are required for
  the requested change.
- Do not create branches, commits, tags, pull requests, issues, releases, or
  deployments unless the user explicitly asks for that action.
- State exactly what was changed and which checks were run. Never claim that a
  command passed if it was not run successfully.

## Project overview

Luminesk-CLI is a Python 3.13+ command-line application whose executable is
`nesk`. It is a reproducible recipe composer and lifecycle manager for
Minecraft Java and Bedrock server instances. Docker is the only runtime
driver.

The core workflow is:

1. A human-authored recipe in `luminesk.toml` declares package metadata,
   sources, templates, ownership, runtime settings, and checks.
2. Resolution produces canonical `luminesk.lock` data containing exact source
   hashes, URLs, recipe revision, target platform, and OCI image digest.
3. Luminesk builds and independently verifies a deterministic
   `.lumineskpkg` transaction boundary.
4. A conflict-free plan is applied transactionally to an instance while
   preserving user-owned data.
5. The instance runs in Docker from the locked image and explicit argv. Failed
   apply or readiness operations restore the previous state when possible.

The important product properties are reproducibility, bounded and verified
I/O, safe path handling, ownership-aware updates, rollback, and stable
`--json --non-interactive` behavior. Treat them as compatibility and security
contracts, not implementation details.

Luminesk 2.0 is not required to preserve the internal formats of Luminesk 1.x.
Do not add legacy compatibility unless the task explicitly requires it.

## Repository map and boundaries

| Path | Responsibility |
| --- | --- |
| `luminesk_cli/cli/` | Argument parsing, lazy dispatch, command handlers, human and JSON output. Keep this layer thin. |
| `luminesk_cli/application/` | Install, lock, update, and runtime use-case orchestration. |
| `luminesk_cli/domain/` | Core models, validation, plans, primitives, and typed errors. Keep it independent of infrastructure concerns. |
| `luminesk_cli/infrastructure/` | Filesystem state, networking, providers, cache, packages, Docker/OCI, templates, and security adapters. |
| `tests/` | Pytest regression and contract tests; reusable recipes live under `tests/fixtures/`. |
| `scripts/` | Formatting, security, packaging, release, cold-path, and end-to-end gates. |
| `docs/` | Docusaurus documentation application and user documentation. |
| `.github/workflows/` | Cross-platform CI, security, documentation, Docker E2E, and release automation. |

Respect the existing dependency direction. In particular, domain code must not
import `filelock`, `httpx`, `platformdirs`, or `rich`; this is enforced by
`scripts/security_gate.py`. Prefer existing abstractions and neighboring
patterns over introducing parallel mechanisms.

## Behavioral and security invariants

Any relevant change must preserve all of these unless the user explicitly asks
to change the contract and the change includes focused tests and documentation:

- The manifest schema is strict: unknown keys are errors, recipe paths are
  portable relative POSIX paths, and `manifest_version` remains distinct from
  the product version.
- Locks bind mutable inputs to exact content hashes and Docker images to
  immutable repository digests. Do not hand-edit generated `luminesk.lock`,
  `.lumineskpkg` metadata, or instance `.luminesk_cli/` state.
- Downloads, provider metadata, recipe snapshots, build contexts, templates,
  archive extraction, log capture, and redirects remain explicitly bounded.
- Remote bytes are verified by SHA-256 before use. HTTPS and public network
  destinations remain the default; redirects are revalidated and credentials
  must not leak across hosts.
- Reject absolute paths, traversal, path escape through symlinks, unsafe
  archive members, hardlinks, and special files wherever the current contract
  rejects them.
- Never introduce `eval`, `exec`, or `subprocess(..., shell=True)`. Commands
  controlled by recipes are argument arrays; do not insert an implicit shell,
  interpolation, pipe, redirection, or host-command capability.
- Preserve transactional install/update behavior: locking, staging, journal,
  backup, atomic state commit, rollback, and evidence needed for recovery.
- Preserve the four ownership modes (`managed`, `generated`, `preserve`, and
  `data`). Never overwrite local drift in managed/generated files or remove
  preserve/data content merely because a later package omits it.
- Secret inputs must come from files, must not have defaults, and must not be
  persisted or exposed in logs, normal JSON, runtime arguments, or checks.
- Docker remains digest-pinned and argument-only. Preserve safe defaults such
  as a read-only container root and instance-contained bind sources.
- Human output may improve without changing automation contracts. Keep JSON
  envelopes, field meanings, exit codes, `--non-interactive` behavior, and
  stdout/stderr separation stable unless the task explicitly changes them.
- Keep behavior portable across supported Linux, macOS, and Windows runners
  and Python 3.13/3.14. Avoid platform assumptions in paths, permissions,
  process handling, and tests.

If a change touches archives, paths, networking, providers, secrets, packages,
transactions, Docker, or workflows, treat it as security-sensitive and run the
additional checks below.

## Environment and dependencies

Use the locked `uv` environment from the repository root:

```bash
uv sync --locked --extra dev
```

- Run Python tools through `uv run`; do not rely on globally installed
  versions.
- `pyproject.toml` and `uv.lock` are authoritative. Do not edit `uv.lock` by
  hand.
- Do not add, remove, or upgrade runtime/development dependencies unless the
  request requires it. If dependencies change, regenerate the lock with `uv`
  and include the lock change in the same task.
- Do not alter tool configuration, exclusions, thresholds, or CI gates merely
  to make a failing change pass.

## Python implementation rules

- Follow PEP 8 and the repository's Ruff configuration. The configured target
  is Python 3.13 and the formatter line length is 88.
- Add precise type annotations to changed code. Keep `ty check .` clean; avoid
  unnecessary `Any`, casts, and untyped escape hatches.
- Do not add `# noqa`, `# type: ignore`, blanket ignores, or weaker checking
  without a narrow, documented reason that is part of the requested task.
- Use modern Python supported by the declared minimum version, but follow the
  local style of the file being edited.
- Keep public contracts and domain models explicit. Prefer typed
  `LumineskError` failures and existing error codes over ad hoc exceptions.
- Catch exceptions only where they can be handled meaningfully. The CLI
  dispatch boundary owns conversion of unexpected exceptions into stable user
  output.
- Keep functions focused, names descriptive, imports ordered, and side effects
  visible. Comments should explain non-obvious reasons or invariants, not
  narrate the code.
- Use `pathlib` and existing safe-path helpers for filesystem work. Never
  weaken containment checks for convenience.
- Use subprocess argument lists with `shell=False`. Check return codes and
  provide useful, sanitized failures.
- Keep import-time work minimal; `nesk --version` and `nesk --help` have a
  cold-path budget and intentionally use lazy imports.

## Tests

- Every bug fix must include a focused regression test that fails before the
  fix and passes after it. Every behavior change needs tests for its public
  success and failure paths.
- Prefer extending the nearest existing test module and fixtures. Create a new
  file only for a distinct behavior area.
- Test observable contracts rather than private implementation details. Use
  temporary directories and deterministic fakes/fixtures; do not depend on a
  developer's real cache, Docker instances, credentials, or network.
- Run the smallest relevant pytest selection while iterating, then the full
  suite before completion.
- Do not delete, skip, loosen, or rewrite a test solely to conceal a
  regression. Do not lower the branch-coverage gate, currently 76%.

Example targeted runs:

```bash
uv run pytest tests/test_manifest.py
uv run pytest tests/test_update_v2.py -k rollback
```

## Mandatory formatting and validation

The repository script is `scripts/format.py` (plural `scripts`), not
`script/format.py`.

After changing Python, format only the Python paths you intentionally edited:

```bash
uv run python scripts/format.py --fix path/to/changed.py tests/test_changed.py
```

Inspect the resulting diff and undo any unrelated formatter churn. Before
finishing any repository change, run the complete quality gate from the root:

```bash
uv lock --check
uv run python scripts/format.py
uv run ruff check .
uv run ruff format --check .
uv run ty check .
uv run pytest --strict-config --strict-markers -W error::ResourceWarning
```

The format script and direct Ruff format check are intentionally both listed
because CI runs both. Ruff validates style/imports and `ty` validates typing;
neither replaces tests.

If a required command cannot run because a tool, service, credential, network,
platform, or Docker daemon is unavailable, do not change the repository to
bypass the limitation. Report the exact command and reason as not run or
blocked.

## Additional gates by change area

Run these in addition to the mandatory gate when applicable:

- Security, archive, network, provider, package, or workflow changes:

  ```bash
  uv run python scripts/security_gate.py
  uv run pytest tests/test_archive_security.py tests/test_fetch_v2.py \
    tests/test_manifest.py tests/test_package_v2.py \
    tests/test_input_security_v2.py tests/test_recipe_acquisition_v2.py \
    tests/test_security_network.py tests/test_security_contracts_v2.py \
    tests/test_sources_v2.py
  ```

- CLI entry point, import graph, `--version`, or `--help` changes:

  ```bash
  uv run python scripts/check_cold_path.py
  ```

- Packaging or distribution metadata changes:

  ```bash
  uv build
  uv run python scripts/verify_wheel.py dist/*.whl
  ```

- Documentation application or content changes:

  ```bash
  pnpm --dir docs install --frozen-lockfile
  pnpm --dir docs audit:security
  pnpm --dir docs check:images
  pnpm --dir docs typecheck
  pnpm --dir docs build
  ```

- Docker lifecycle changes: run `uv run python scripts/docker_e2e.py` only in a
  disposable environment with a working Docker daemon. Run
  `uv run python scripts/catalog_acceptance.py` only when the task requires the
  official, network-backed catalog path.

Do not run publishing, deployment, release, or real-instance lifecycle
commands as validation.

## Documentation and public contracts

- If requested behavior changes the CLI, manifest, lockfile, recipe semantics,
  ownership, runtime, security model, or update flow, update the relevant file
  under `docs/docs/` and any affected README example in the same coherent
  change.
- Do not duplicate the full documentation in comments or tests. Link concepts
  through the existing docs structure and keep examples executable and
  consistent with parser behavior.
- Use the repository's established English terminology and Markdown style.
- Do not edit release notes, version fields, changelogs, or migration guidance
  unless the task explicitly includes a release or compatibility change.

## Prohibited actions

Unless the user explicitly requests and authorizes the exact action, do not:

- rename, move, delete, or mass-format unrelated files;
- broaden a fix into a refactor or redesign;
- change CLI flags, JSON schemas, exit codes, manifest/lock formats, ownership
  rules, security limits, or compatibility guarantees;
- weaken validation, checksums, path/network restrictions, rollback, typing,
  linting, tests, coverage, or pinned GitHub Action revisions;
- add shell execution, dynamic code execution, unbounded I/O, secret logging,
  or a force path around ownership conflicts;
- modify dependencies, lockfiles, generated state, fixtures, snapshots, or
  golden outputs without a task-driven reason;
- operate on a real Minecraft instance, prune a real cache, start/stop real
  containers, exercise provider APIs with real credentials, or perform writes
  against external services;
- run destructive Git commands, rewrite history, force-push, publish packages,
  deploy documentation, create tags/releases, or merge pull requests.

When such an action is genuinely necessary, explain its scope and risk and ask
before proceeding.

## Completion checklist

Before responding to the user:

1. Confirm the final diff contains only task-related changes.
2. Confirm new behavior has focused tests and affected documentation is
   accurate.
3. Run the mandatory formatting, Ruff, ty, and pytest gates plus applicable
   specialized gates.
4. Check `git diff --check` and the working-tree status.
5. Summarize changed files, observable behavior, validation results, and any
   remaining limitation without overstating completion.
