---
sidebar_position: 6
---

# Catalog, Recipes, and Trust

A recipe controls downloaded artifacts, generated files, optional Docker build
instructions, container image/argv, mounts, ports, and user-data policy. Treat
every recipe as code with access to the instance paths it mounts—even when it
comes from the official catalog.

Luminesk reduces ambiguity and verifies content. It cannot decide whether a
maintainer, server binary, container image, or network endpoint deserves your
trust.

## The official catalog

The catalog is a locally cached, verified snapshot of
`farin-works/luminesk-database`. Start with:

```bash
nesk catalog update
nesk catalog status
nesk search --type core
nesk search lumi --edition bedrock
nesk info RECIPE
```

Existing 2.x lockfiles and cached snapshots with the
`github:task-v1/luminesk-database` identity remain readable;
their pinned revisions and hashes are preserved. Do not edit instance state or
lockfiles to replace the old name. Run `nesk catalog update` to refresh discovery.

`catalog update` resolves the official repository's default branch to a commit,
downloads a bounded index and its SHA-256, validates its strict schema, and
activates it crash-safely. The index binds each entry to a package identity,
recipe path, manifest digest, and optional template digest at an exact database
content revision.

`search` uses only the active local snapshot. `info` does not refresh that
snapshot, but it loads the selected entry's manifest at the exact revision and
verifies the manifest digest before showing its complete input contract. That
manifest is content-cached, so later inspection can remain offline. Reverify or
deliberately select cached state with:

```bash
nesk catalog verify
nesk catalog use 0123456789abcdef0123456789abcdef01234567
```

`catalog use` accepts an exact cached 40-character lowercase commit; it does not
download a missing revision. This lets an operator return discovery to an
already reviewed snapshot.

The catalog verifies origin and bytes. “Official” does not mean that every
runtime image, upstream artifact, build, port, or data policy is appropriate
for your host.

## Recipe origins

`nesk install SOURCE` supports three trust origins:

| Origin | Example | Acquisition and update policy |
| --- | --- | --- |
| Official database | `nesk install lumi` or `db:lumi` | Manifest/template digests must match the active verified catalog entry. The entry tracks future active-catalog changes. |
| Direct GitHub | `nesk install owner/repo@main` | GitHub ref resolves to an exact commit. A branch is tracked; a tag/commit is pinned. |
| Local | `nesk install ./recipe` | Declared local assets are snapshotted and the origin is untracked. Later edits in the source directory are not followed by instance update. |

Direct GitHub forms also include `github:OWNER/REPO`, an HTTPS GitHub URL, and
`--ref REF`. Git is not executed. Luminesk uses API metadata and an exact
commit-pinned acquisition:

- recipes without `[build]` fetch only `luminesk.toml` and declared template,
  `[[files]]`, and local-source assets;
- a recipe with `[build]` may need its bounded full source context, which is
  cached for the exact commit;
- the installed instance receives only the canonical declared recipe snapshot,
  not unrelated repository tests/source files.

## Pinning and tracking

Every installed remote recipe lock stores exact revision, package version,
manifest digest, and template digest. Tracking only decides how a future
connected update obtains a candidate:

- official database recipes consult the same named entry in the active catalog;
- direct GitHub branches resolve the branch again;
- direct tags/commits reuse the pinned recipe (while its own mutable artifact
  providers or image tags may still resolve newer content);
- local recipes reuse the installed immutable snapshot.

For a catalog recipe, run `catalog update` before `outdated` when you intend to
consider new database content. A catalog's unrelated revision change is not
itself a recipe update when the selected entry's identity/digests are unchanged.

Direct GitHub recipe content that changes without a `package.version` bump
produces a warning. Treat that as a maintainer error and review the full diff.

## Confirmation model

Remote and external local installs print a unified preview after resolution,
package construction, and conflict planning. It includes origin, exact
revision/version, source types and resolved artifact digests, locked runtime
image, runtime user/mount/port capabilities, build/network status, ownership,
checks, and every planned file change. JSON exposes the same data under
`preview`. Runtime command, memory, user, mounts, and ports are shown after
declared non-secret input values and defaults have been resolved.

```bash
nesk install RECIPE --dir ./instance --dry-run
nesk install RECIPE --dir ./instance --yes
```

`--dry-run` prevents instance writes, but recipe/artifact metadata can still be
fetched and verified caches populated. It displays the complete plan without
asking for trust approval. `--json` cannot prompt; a real remote install therefore
needs `--yes --json --non-interactive` after the caller has approved the dry-run
payload.

`--yes` approves one displayed plan. It does not weaken schema, hash,
ownership, or transaction checks.

## Review checklist

Before approving a new or changed recipe, inspect:

1. `package.repository`, exact recipe ref, maintainer, and license.
2. Every provider type/endpoint, mutable selector, `max_size`, extraction, and
   `allow_http`/`allow_private_network` exception.
3. `[build]` Dockerfile and whether `build.network = true` is justified.
4. Runtime image repository/digest change and its own supply-chain policy.
5. Exact argv, `run_as`, read-only root, mounts, and published ports.
6. Templates and inputs, especially secret-bearing output.
7. `managed`, `generated`, `preserve`, and `data` classifications.
8. Readiness condition, timeout, backup paths, and retention.
9. `nesk diff` and `nesk update --dry-run --json` output on later updates.

There is no `[permissions]` or host-command option. A `[build]` table permits a
Docker build; `build.network = true` permits its default Docker network. Runtime
and command checks run inside the selected container through explicit argv.

## Network and provider trust

Remote URLs default to HTTPS and public addresses. Redirect destinations are
rechecked, credentials are stripped on cross-host redirects, metadata and
downloads are bounded, and final bytes are hashed. A recipe that sets
`allow_http` or `allow_private_network` intentionally widens that boundary.

`GITHUB_TOKEN` and `GITLAB_TOKEN` can authorize supported metadata requests.
Run Luminesk with least-privilege tokens and avoid exposing them to unreviewed
endpoints. Jenkins has no manifest-level credential integration.

See [Source Providers](/docs/sources) for exact network/version behavior and
[Templates and Inputs](/docs/templates-and-inputs) for secret handling.

## Build and runtime trust

A Dockerfile build has bounded context/time/CPU/memory, no host mounts or
Docker socket added by Luminesk, digest-pinned base images, and no network by
default. Nevertheless, it executes untrusted build instructions inside the
local Docker daemon and produces files that enter the package.

The runtime image and server binary execute with access to declared bind mounts
and published ports. `read_only_root = true` limits the container root but an
`rw` instance mount remains writable. Use `run_as`, ownership, firewall, and
host isolation suitable for untrusted server code.

## Third-party recipes

Direct GitHub and local recipes receive the same parser, resolver, hash,
package, and transaction checks as catalog recipes. What they do not receive is
the official catalog's fixed repository/index/entry binding. Pin a tag or
commit for stability, review changes before moving a tracked branch, and keep
production backups outside the instance.

Continue with [Updating Instances](/docs/updating-instances) for the production
review/backup/rollback sequence.
