---
sidebar_position: 2
---

# `luminesk.toml` Reference

This page documents manifest schema version 1 as accepted by the current
loader. The manifest must be UTF-8, named exactly `luminesk.toml`, and no larger
than 1 MiB. Unknown keys are rejected everywhere except vendor tables below
`[x]`.

## Minimal manifest

Place this manifest and a real `server.jar.in` file in the same directory. It
can be statically validated, locked, packaged, and installed; replace the image
and command when the artifact needs a different runtime.

```toml
manifest_version = 1

[package]
name = "local-java-core"
version = "1.0.0"
kind = "core"
game = "minecraft"
edition = "java"

[[sources]]
id = "core"
type = "local-file"
target = "server.jar"

[sources.options]
path = "server.jar.in"
version = "1.0.0"

[runtime]
image = "eclipse-temurin:21-jre"
command = ["java", "-jar", "server.jar", "nogui"]
```

`[package]` and `[runtime]` are always required. A recipe may omit `sources`,
`build`, `template`, and `files` when the pinned runtime image contains the
complete server; the resulting package contains metadata but no payload files.

## Top-level fields

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `manifest_version` | integer | yes | — | Must be exactly `1`; this is the schema version, not the product version. |
| `template` | string | no | none | Portable relative path to a recipe directory copied into the package. Files ending in `.tmpl` are rendered and lose that suffix. |
| `package` | table | yes | — | Package identity and catalog metadata. |
| `inputs` | table of tables | no | `{}` | Named string, integer, or boolean inputs. |
| `sources` | array of tables | no | `[]` | Artifacts resolved and copied or extracted into the package. |
| `files` | array of tables | no | `[]` | Local recipe files/directories copied into the package. |
| `ownership` | table | no | empty lists | Recursive preserve, data, and executable path policies. |
| `runtime` | table | yes | — | Docker image, argv, mounts, ports, and resource policy. |
| `build` | table | no | none | Isolated Dockerfile build whose selected output is packaged. |
| `checks` | array of tables | no | `[]` | Post-build, post-install, and readiness checks. |
| `update` | table | no | transactional defaults | Backup retention and rollback policy. |
| `x` | table of tables | no | `{}` | Vendor extension data. Every direct `x.<vendor>` value must be a table; Luminesk otherwise leaves it uninterpreted. |

There is no `[permissions]` table and no host-command switch. A manifest that
declares either is invalid. A `[build]` table opts into a Docker build;
`build.network = true` explicitly gives that build Docker's default network.

## Portable paths

Recipe and instance paths must be normalized, relative, use `/`, and avoid
`.`/`..` segments, Windows drive/UNC forms, control characters, trailing dots
or spaces, and Windows reserved names such as `CON` or `NUL`. Symlinks and
special files are rejected when recipe assets, templates, build contexts, and
packages are collected.

The literal `.` is allowed only for an extracted source target and a runtime
mount source. Container locations such as `build.output` are documented
separately.

## `[package]`

```toml
[package]
name = "lumi-bedrock"
version = "1.2.0"
display_name = "Lumi for Bedrock Edition"
kind = "core"
game = "minecraft"
edition = "bedrock"
summary = "A Lumi server recipe"
keywords = ["lumi", "bedrock", "plugins"]
license = "GPL-3.0-or-later"
authors = ["Example Maintainer"]
platforms = ["linux/amd64", "linux/arm64"]

[package.repository]
url = "https://github.com/example/lumi-recipe"
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `name` | string | yes | — | Lowercase package identifier, 1–128 characters; starts/ends alphanumeric and may contain `.`, `_`, or `-`. |
| `version` | string | yes | — | Semantic version with three numeric components, optional prerelease, and optional build metadata. Mutable words such as `latest` are invalid here. |
| `display_name` | string | no | none | Human-readable catalog/UI name. |
| `kind` | string enum | yes | — | `core` or `template`. This is package classification, not a different schema. |
| `game` | string enum | yes | — | Currently only `minecraft`. |
| `edition` | string enum | yes | — | `java`, `bedrock`, or `cross-platform`. |
| `summary` | string | no | `""` | Short description; an empty string is accepted. |
| `keywords` | array of strings | no | `[]` | Discovery terms. |
| `license` | string | no | none | Recipe/package license identifier or label. |
| `authors` | array of strings | no | `[]` | Maintainer/author labels. |
| `platforms` | array of strings | no | `[]` | Allowed host targets in `os/architecture` form. Empty means all supported targets. Locking fails if the current target is absent. |
| `repository` | table | no | none | Contains the recipe repository metadata. |
| `repository.url` | HTTPS URL | yes if table exists | — | Absolute HTTPS URL without embedded credentials or fragment. |

Supported CLI hosts are `linux/amd64`, `linux/arm64`, `darwin/amd64`,
`darwin/arm64`, and `windows/amd64`. Recipe targets follow the Docker Engine's
platform, independently of the CLI host.

## `[inputs.<name>]`

```toml
[inputs.server_name]
type = "string"
default = "Minecraft Server"
prompt = "Server name"
pattern = "^.{1,80}$"

[inputs.port]
type = "integer"
default = 19132
min = 1
max = 65535

[inputs.eula]
type = "boolean"
required = true
prompt = "Accept the Minecraft EULA"

[inputs.api_token]
type = "string"
required = true
secret = true
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `type` | string enum | yes | — | `string`, `integer`, or `boolean`. |
| `default` | matching scalar | no | none | Must match `type`. Forbidden when `secret = true`. |
| `prompt` | string | no | none | Human-facing label shown by `nesk info` and the interactive install/update input wizard. Automation never prompts. |
| `min` | integer | no | none | Inclusive minimum applied to integer values. Cannot exceed `max`; an integer default is checked immediately. |
| `max` | integer | no | none | Inclusive maximum applied to integer values. |
| `pattern` | string | no | none | Python regular expression, compiled at manifest load and matched against the complete string value. |
| `required` | boolean | no | `false` | Package building fails if neither an override nor a default supplies the input. |
| `secret` | boolean | no | `false` | Requires `--set-file`; cannot have a default, is not persisted, and may only be referenced by rendered files. |

Use interpolation as a complete or partial string:

```toml
memory = "${input.memory}"
command = ["java", "-Xmx${input.memory}", "-jar", "server.jar"]
```

References use `${input.NAME}` where `NAME` consists of letters, digits,
underscore, or hyphen. Runtime, mount, port, and check interpolation is rejected
for secret inputs. Template rendering converts booleans to lowercase `true` or
`false`.

## `[[sources]]`

```toml
[[sources]]
id = "core"
type = "maven"
target = "server.jar"
max_size = 536870912
extract = false
platforms = ["linux/amd64", "linux/arm64"]
allow_http = false
allow_private_network = false

[sources.options]
repository = "https://repo.lumi.su/releases"
group = "com.koshakmine"
artifact = "Lumi"
version = "latest"
channel = "stable"
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `id` | string | yes | — | Unique lowercase package identifier used as an update component and lock key. |
| `type` | string enum | yes | — | `http`, `maven`, `jenkins`, `github-release`, `github-source`, `gitlab-release`, `gitlab-job-artifact`, `mojang-version`, `paper`, or `local-file`. |
| `target` | path | yes | — | Unique package-relative destination. `.` is allowed only with `extract = true`. Targets from multiple contributors may not collide. |
| `options` | table | yes | — | Provider-specific strict table; see [Source Providers](/docs/sources). |
| `max_size` | integer | no | `536870912` | Maximum downloaded/decoded bytes (512 MiB); must be at least 1. |
| `extract` | boolean | no | `false` | Safely extract a ZIP/TAR-like artifact into `target`. Required for `github-source`. |
| `platforms` | array of strings | no | `[]` | Resolve this source only for matching `os/architecture` targets. |
| `allow_http` | boolean | no | `false` | Permit plain HTTP URLs for providers with a configurable URL. HTTPS remains the safe default. |
| `allow_private_network` | boolean | no | `false` | Permit loopback/private/link-local destinations. This expands the recipe's network trust boundary. |

Every resolved source records the selected version/revision, final URL, actual
size, SHA-256, media type when known, and target in the lock. `local-file`
requires a regular file inside the recipe root and is hashed into the same
content cache.

## `[[files]]`

```toml
[[files]]
source = "config/welcome.txt.tmpl"
target = "welcome.txt"
mode = "generated"
template = true
executable = false

[[files]]
source = "seed/world"
target = "world"
mode = "data"
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `source` | path | yes | — | File or directory inside the recipe root. May be absent only when `mode = "data"`; that declaration creates an empty target directory. |
| `target` | path | yes | — | Unique package-relative destination. |
| `mode` | string enum | no | `managed` | `managed`, `preserve`, `generated`, or `data`. |
| `template` | boolean | no | `false` | Render `${input.*}` in each UTF-8 source file. Unlike the top-level template tree, the filename is not changed automatically. |
| `executable` | boolean | no | `false` | Add the owner executable bit to the target file/path. |

A directory source is copied recursively. Symlinks and special files are
rejected. All target collisions—between build output, sources, the template
tree, and declared files—are errors.

`template = true` renders a complete target file; it does not merge properties
with an existing or server-generated configuration. Do not ship partial
server-owned configs. Prefer letting the server generate the full file and
protecting the path with `preserve`; if seeding is required, provide a complete
upstream-compatible file.

## `[ownership]`

```toml
[ownership]
preserve = ["server.properties"]
data = ["world", "world_nether", "world_the_end", "plugins"]
executable = ["bin/start-helper"]
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `preserve` | array of paths | no | `[]` | Recursively mark matching packaged paths as preserve. |
| `data` | array of paths | no | `[]` | Recursively mark matching packaged paths as user data. |
| `executable` | array of paths | no | `[]` | Recursively add the owner executable bit. |

Paths are literal prefixes, not globs. Duplicates are rejected. `preserve` and
`data` paths may not overlap each other. These policies override the default
ownership assigned to any matching packaged output.

## `[runtime]`

```toml
[runtime]
image = "eclipse-temurin:21-jre"
command = ["java", "-Xms${input.memory}", "-Xmx${input.memory}", "-jar", "server.jar", "nogui"]
workdir = "/server"
memory = "${input.memory}"
stop_signal = "SIGINT"
stop_timeout = 30
restart = "unless-stopped"
restart_limit = 0
run_as = "1000:1000"
read_only_root = true
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `image` | string | yes | — | OCI image tag or pinned digest. Locking resolves a tag to `repository@sha256:...`; installed runtime always uses the lock value. |
| `command` | non-empty array of strings | yes | — | Arguments appended after the image. No shell is inserted; metacharacters remain literal argument content. |
| `workdir` | string | no | `/server` | Docker container working directory. Use an absolute container path. |
| `memory` | string | no | none | Docker memory limit, optionally interpolated. At start it must be a positive integer plus optional `b`, `k`, `m`, or `g` suffix. |
| `stop_signal` | string | no | `SIGINT` | Passed as Docker's container stop signal. |
| `stop_timeout` | integer | no | `30` | Seconds passed to `docker stop`; minimum 1. |
| `restart` | string enum | no | `no` | Runtime accepts `no`, `on-failure`, `always`, or `unless-stopped`. |
| `restart_limit` | integer | no | `0` | Non-negative retry limit appended only to `on-failure` when nonzero. |
| `run_as` | string | no | none | Docker user/group expression such as `1000:1000`. Ensure mounted files are accessible to it. |
| `read_only_root` | boolean | no | `true` | Add Docker's `--read-only`; writable server state must be under an `rw` bind mount. |

If `runtime.mounts` is empty, Luminesk bind-mounts the instance root (`.`) at
`workdir` read-write. Declaring any mounts replaces that implicit default.

### `[[runtime.mounts]]`

```toml
[[runtime.mounts]]
source = "."
target = "/server"
mode = "rw"
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `source` | path | yes | — | Instance-relative path or `.`. Resolution must remain inside the instance; non-dot paths are created as directories before start. |
| `target` | string | yes | — | Container bind destination. Use an absolute path accepted by Docker. |
| `mode` | string enum | no | `rw` | `rw` or `ro`. |

### `[[runtime.ports]]`

```toml
[[runtime.ports]]
name = "game"
host = "${input.port}"
container = 25565
protocol = "tcp"
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `name` | string | yes | — | Human-readable mapping name. |
| `host` | integer or input reference | yes | — | Host port, 1–65535 after interpolation. |
| `container` | integer or input reference | yes | — | Container port, 1–65535 after interpolation. |
| `protocol` | string enum | no | `tcp` | `tcp` or `udp`. |

String port values must be exactly `${input.name}`; arbitrary numeric strings
or mixed interpolation are rejected by the manifest loader. Container bindings
must be unique within each protocol.

## `[build]`

```toml
[build]
file = ".luminesk/Dockerfile"
output = "/out"
timeout = 1200
cpu = 2
memory = "2g"
network = false
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `file` | path | yes | — | Dockerfile inside the bounded recipe context. |
| `output` | string | yes | — | Absolute path copied from the finished build image into package staging. |
| `timeout` | integer | no | `1200` | Docker build timeout in seconds; minimum 1. |
| `cpu` | integer | no | `2` | CPU quota in whole CPUs; minimum 1. |
| `memory` | string | no | `2g` | Value passed to `docker build --memory`. |
| `network` | boolean | no | `false` | `false` uses `--network none`; `true` uses Docker's default build network. |

All external `FROM` images are resolved to repository digests and rewritten in
a temporary Dockerfile. Dynamic `FROM $VARIABLE` is forbidden. A custom
`# syntax=` frontend must already be SHA-256 pinned. The copied context excludes
`.git`, `.luminesk_cli`, `worlds`, and `logs`, rejects links/special files, and
is bounded to 20,000 files and 1 GiB.

## `[[checks]]`

```toml
[[checks]]
id = "ready"
phase = "readiness"
kind = "log-regex"
pattern = "Done .* For help, type"
timeout = 120
required = true
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `id` | string | yes | — | Unique check identifier. |
| `phase` | string enum | yes | — | `post-build`, `post-install`, or `readiness`. |
| `kind` | string enum | yes | — | `file`, `process-alive`, `log-regex`, `tcp`, or `command`, subject to phase restrictions below. |
| `required` | boolean | no | `true` | A failed required check aborts its operation; optional readiness timeout continues. |
| `path` | path | for `file` | none | Package/instance-relative file required by a file check. |
| `pattern` | string | for `log-regex` | none | Python regular expression searched in accumulated Docker logs. |
| `host` | string | no | `127.0.0.1` for TCP | TCP readiness host after interpolation; only `localhost` or a loopback address is allowed. |
| `port` | integer or input reference | for useful `tcp` checks | none | TCP port, validated after interpolation. |
| `command` | non-empty array of strings | for `command` | `[]` | Argv executed by `docker exec`; no shell. |
| `timeout` | integer | no | `30` | Readiness polling deadline in seconds; minimum 1. |

`post-build` and `post-install` currently accept only `file`. Readiness rejects
`file` and supports `process-alive`, `log-regex`, `tcp`, and `command`. Checks
run in declaration order. If no readiness checks exist, start uses a default
five-second `process-alive` check.

## `[update]`

```toml
[update]
strategy = "transactional"
backup = ["world", "world_nether", "world_the_end", "plugins", "server.properties"]
retain_backups = 3
rollback_on_failure = true
```

| Field | Type | Required | Default | Meaning and constraints |
| --- | --- | --- | --- | --- |
| `strategy` | string enum | no | `transactional` | The only supported value is `transactional`. |
| `backup` | array of paths | no | `[]` | Additional existing files/directories copied into each transaction backup. Missing and symlink paths are skipped. |
| `retain_backups` | integer | no | `3` | Number of newest backup directories retained after a successful transaction; minimum 0. |
| `rollback_on_failure` | boolean | no | `true` | Declared policy value. Current transactions always attempt rollback when apply/readiness fails. Keep this `true`; do not treat `false` as a way to disable safety. |

Transaction backup also includes replaced/removed package paths and prior lock,
state, ownership, and recipe metadata. This is operation rollback, not a
substitute for independently tested backups of production worlds.

## `[x.<vendor>]`

```toml
[x.example]
catalog_note = "metadata for external tooling"
```

The top-level `x` value and every direct child must be a table. Contents below
that child are not validated or interpreted by Luminesk. Do not use extensions
to imply runtime permissions or behavior that the public schema does not have.

## Validate a manifest

```bash
nesk validate --dir ./recipe --static
nesk validate --dir ./recipe --resolve
nesk validate --dir ./recipe --build --set eula=true
nesk lock --dir ./recipe
nesk plan --dir ./recipe --set eula=true
```

`--resolve` includes static validation and performs provider/image resolution.
`--build` also creates a temporary package and accepts repeatable `--set` and
`--set-file` inputs. A recipe with required inputs and no defaults must provide
them to exercise rendering and package assembly.
