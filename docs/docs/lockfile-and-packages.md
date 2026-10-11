---
sidebar_position: 1
---

# Lockfile and Packages

`luminesk.toml`, `luminesk.lock`, `.lumineskpkg`, and an installed instance are
four different artifacts:

| Artifact | Owner | Mutable? | Purpose |
| --- | --- | --- | --- |
| `luminesk.toml` | Recipe author | yes, deliberately | Human-reviewed desired configuration. |
| `luminesk.lock` | Luminesk resolver | regenerate, do not hand-edit | Exact platform, sources, images, recipe origin, and hashes. |
| `.lumineskpkg` | Luminesk package builder | immutable | Deterministic verified payload and metadata used as a transaction boundary. |
| Installed instance | Operator + server + Luminesk | operationally mutable | Running payload, user data, manifest/lock, ownership, backups, and state. |

## Why the lockfile exists

Recipe fields such as `latest`, version constraints, branch names, artifact
URLs, and image tags can change. A lock converts them into a reviewable result:

- `lockfileVersion` — currently `1`;
- `manifestDigest` — SHA-256 of the exact `luminesk.toml` bytes;
- `target` — current normalized host such as `linux/amd64`;
- `sources` — one record per source selected for that target;
- `runtime.image` — full OCI repository digest;
- `build.images` — original Dockerfile base references mapped to digests;
- `recipe` — local, direct GitHub, or official database origin when the
  workflow snapshots a complete recipe.

Whitespace and comment edits change `manifestDigest` because it binds exact
bytes, not only parsed meaning.

## Source records

Each `sources.ID` lock record contains:

| Field | Meaning |
| --- | --- |
| `type` | Provider used by the manifest. |
| `version` | Provider-selected/user-facing version. |
| `sourceRevision` | Provider's immutable build/commit/version identity where available. |
| `url` | Resolved artifact URL, or `local:PATH`. |
| `size` | Actual fetched bytes. |
| `digest` | Actual lowercase SHA-256. |
| `target` | Bound package destination. |
| `mediaType` | Optional provider-reported media type. |

The content cache is addressed by `digest`, not by URL. Cache restoration
rehashes a blob; a corrupt entry is rejected (and normal restore removes the
bad file). `nesk cache verify` audits all cached blobs.

## Recipe origin and revision

When created by `nesk lock` or install, the optional `recipe` object binds:

- `kind`: `database`, `github`, or `local`;
- canonical `source` and exact `revision`;
- `ref` and `tracking` policy;
- catalog `entry`/`path` for official database recipes;
- recipe `version`, `manifestDigest`, and optional `templateDigest`.

Official catalog entries follow the active verified database entry. A direct
GitHub branch/default branch is tracked but its installed revision is still an
exact commit. A direct tag/commit is pinned rather than tracked. Local recipes
are untracked snapshots identified by manifest content.

Installed instances keep a canonical recipe snapshot under
`.luminesk_cli/recipe/` plus a root `luminesk.toml` mirror. Runtime and update
commands verify them against the lock before trusting the recipe.

## Create and review a lock

For a recipe source directory:

```bash
nesk validate --dir ./recipe --static
nesk lock --dir ./recipe
nesk cache verify
```

`nesk lock` resolves source metadata, downloads/hashes selected artifacts,
resolves the runtime image and Dockerfile base images, records the current
Docker Engine target, and writes canonical sorted JSON crash-safely. Commit the manifest and
lock together when publishing a local recipe workflow that needs a reviewed
resolution.

The target is the container platform, such as `linux/amd64` on Windows with
Docker Desktop's Linux backend. It controls source selection, image pulls,
builds and startup; it is not the host platform. Existing locks retain their
target. A different engine architecture requires the matching Docker context
or a separate recipe directory for a newly resolved lock. Frozen operations
still query the local engine to verify compatibility, without resolving new
source versions or consulting a registry for already pinned images.

For an installed instance, use `outdated`, `diff`, and `update`; do not replace
its applied lock by running an authoring `nesk lock` in place. The applied lock
must continue to match instance state and the installed recipe origin.

## Frozen and offline operation

```bash
nesk lock --dir ./recipe --frozen
nesk plan --dir ./recipe --frozen
nesk install SOURCE --dir ./instance --frozen --yes
nesk update --dir ./instance --frozen --dry-run
```

Frozen mode checks, as applicable:

- exact manifest digest;
- current target equals lock target;
- exact recipe origin/snapshot matches;
- every locked source digest is available in the verified content cache;
- a remote install locator has a matching verified recipe/lock cache entry.

It does not fetch missing content or silently re-resolve a mismatch. OCI image
digests are already immutable in the lock, but the Docker daemon must still
have/pull what runtime operations need; frozen source resolution is not a
general Docker offline guarantee.

Populate the recipe and content caches during a connected, reviewed workflow
before relying on frozen deployment. Use `cache prune --dry-run` cautiously,
because pruning a required source makes later frozen work fail.

## Regeneration and updates

Regenerate a development recipe lock with `nesk lock`, review changes to every
URL/version/hash/image, and exercise `nesk plan`. For installed instances:

```bash
nesk outdated --dir ./instance
nesk diff --dir ./instance
nesk update --dir ./instance --dry-run
nesk update --dir ./instance --yes
```

An update resolves a candidate lock, builds its package, computes ownership
actions, and commits the new manifest/lock/state only through the transaction.
`--frozen` uses the existing exact lock and is useful for reproducible rebuild
or input-only package application, not discovering a newer artifact.

## What `.lumineskpkg` is

A `.lumineskpkg` is a ZIP containing canonical `metadata.json` and any declared
payload entries under `payload/`. An image-only recipe can produce a valid
metadata-only package with no payload tree. Metadata binds the package to:

- format version `1`;
- package name/version;
- manifest digest and lock digest;
- target platform and optional recipe revision;
- every path's file/directory type, portable mode, byte size, SHA-256, and
  ownership mode.

It is the immutable object that the installer plans and applies. It does not
contain mutable instance state or an operator's existing world.

## Determinism and verification

For identical recipe assets, resolved cache bytes, lock, inputs, target, and
Luminesk implementation, package construction is deterministic: metadata keys
and paths are sorted, JSON is canonical, ZIP timestamps are fixed, normal
permissions are canonicalized, and each payload file is hashed. The test suite
builds the same package twice and asserts equal package digests.

Verification rejects duplicate/extra/missing archive members, unsafe paths,
links/special files, metadata/payload disagreement, size/digest mismatches,
excessive files/expanded size, and suspicious compression ratios. Package
creation verifies the result immediately; extraction verifies it again before
writing staging content. The installer separately checks that manifest, lock,
package, and target bindings agree.

## How users encounter packages

`nesk validate --build`, `plan`, `install`, and `update` build and verify a
temporary `.lumineskpkg`. `plan` exposes its intended create/replace/preserve/
remove/conflict actions; install/update apply it transactionally.

There is currently no public `nesk package` command and no public command that
accepts an arbitrary `.lumineskpkg` path for installation. Do not document or
script either workflow. Treat the format as a visible reproducibility and
transaction concept managed by the current CLI.

## Installed state is not the lock

The instance adds `.luminesk_cli/state.json`, `ownership.json`, recipe snapshot,
transaction journal/backups, and readiness logs. State records the applied lock
digest, installed package digest, runtime container, timestamps, and persisted
non-secret inputs. Editing lock/state by hand breaks those bindings; use
`nesk diff`, `validate --instance`, `recover`, or `import` according to the
problem instead.
