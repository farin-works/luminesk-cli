---
sidebar_position: 1
---

# Creating a Custom Recipe

This tutorial builds a small Lumi Bedrock core recipe. It demonstrates the
manifest contract and test workflow; the source selector, image,
memory, and ownership list are examples derived from the repository's reference
fixtures, not guaranteed production recommendations. Review upstream support,
licenses, EULA terms, ports, and resource needs for your deployment.

## 1. Create the recipe directory

```bash
nesk init --dir ./my-lumi-core --name my-lumi-core
```

`init` creates a valid-shaped starter whose artifact URL is intentionally
non-working. Replace that manifest with the example below:

```text
my-lumi-core/
└── luminesk.toml
```

The finished manifest is shown below, followed by an explanation of each
design decision.

```toml
manifest_version = 1

[package]
name = "my-lumi-core"
version = "0.1.0"
display_name = "My Lumi Core"
kind = "core"
game = "minecraft"
edition = "bedrock"
summary = "A Lumi Bedrock server maintained by Example Org"
keywords = ["lumi", "bedrock", "plugins"]
license = "MIT"
authors = ["Example Org"]

[package.repository]
url = "https://github.com/example/my-lumi-core"

[inputs.memory]
type = "string"
default = "4g"
prompt = "Java heap and container memory limit"
pattern = "^[1-9][0-9]*[mMgG]$"

[inputs.port]
type = "integer"
default = 19132
min = 1
max = 65535

[[sources]]
id = "core"
type = "maven"
target = "server.jar"
max_size = 536870912

[sources.options]
repository = "https://repo.lumi.su/releases"
group = "com.koshakmine"
artifact = "Lumi"
version = "latest"
channel = "stable"

[[files]]
source = "data/worlds"
target = "worlds"
mode = "data"

[[files]]
source = "data/players"
target = "players"
mode = "data"

[[files]]
source = "data/behavior_packs"
target = "behavior_packs"
mode = "data"

[[files]]
source = "data/plugins"
target = "plugins"
mode = "data"

[[files]]
source = "data/resource_packs"
target = "resource_packs"
mode = "data"

[[files]]
source = "data/tmp"
target = ".tmp"
mode = "data"

[ownership]
preserve = ["settings.yml"]
data = [".tmp", "worlds", "plugins", "players", "behavior_packs", "resource_packs"]

[runtime]
image = "eclipse-temurin:21-jre"
command = ["java", "-Xms${input.memory}", "-Xmx${input.memory}", "-jar", "server.jar"]
workdir = "/server"
memory = "${input.memory}"
stop_signal = "SIGINT"
stop_timeout = 30
restart = "unless-stopped"
read_only_root = true

[[runtime.mounts]]
source = "."
target = "/server"
mode = "rw"

[[runtime.mounts]]
source = ".tmp"
target = "/tmp"
mode = "rw"

[[runtime.ports]]
name = "game"
host = "${input.port}"
container = 19132
protocol = "udp"

[[checks]]
id = "core-present"
phase = "post-build"
kind = "file"
path = "server.jar"

[[checks]]
id = "ready"
phase = "readiness"
kind = "log-regex"
pattern = "Done .* For help, type"
timeout = 120
required = true

[update]
strategy = "transactional"
backup = ["worlds", "plugins", "players", "behavior_packs", "resource_packs", "settings.yml"]
retain_backups = 3
rollback_on_failure = true
```

## 2. Describe identity and inputs

`package.version` versions your recipe. Bump it whenever published recipe
content changes so operators can distinguish revisions. It is separate from
the Lumi artifact selected by the `core` source.

The inputs have usable defaults and validation. The port input selects the
host-side Docker UDP port; Lumi listens on container port `19132`. The memory
input is used both in Java's argv and Docker's memory limit. Lumi serves Bedrock
clients even though its runtime is Java.

This recipe has no EULA input or template. There is intentionally no
`settings.yml` template: Lumi creates its complete configuration on first
start. A partial template would replace that whole file, not merge individual
settings into an upstream default.

## 3. Resolve the server binary

The `maven` provider resolves `com.koshakmine:Lumi` from
`https://repo.lumi.su/releases`, as in the
[official Lumi recipe](https://github.com/farin-works/luminesk-database/blob/main/database/lumi/luminesk.toml).
`version = "latest"` with `channel = "stable"` selects the highest matching
stable version at lock time. The lock records the exact version, URL, size,
and SHA-256. When the repository supplies a SHA-256 sidecar, Luminesk verifies
it; otherwise, it hashes the downloaded bytes. To select a release deliberately,
replace `latest` with an exact published version.

The source target `server.jar` is a package-relative destination. It is also
the path used by the Java argv and the post-build file check.

## 4. Define templates and user data

The ownership declaration reserves `settings.yml` for the server and operator.
Because the recipe does not package that path, Lumi can create the complete
file on first start and later package updates leave it alone.

Each `mode = "data"` file declaration above names a source directory that does
not need to exist. For data mode only, an absent source creates an empty target
directory. Lumi can then populate worlds and plugins without making their
contents Luminesk-managed. If you want seed data, create the corresponding
directory in the recipe and put regular files in it.

If you add a top-level `template` tree, its output is `generated` by default.
Every `.tmpl` file must be UTF-8; Luminesk renders `${input.NAME}` placeholders
and removes the suffix. Template rendering always produces a complete file; it
never merges individual properties into a server-owned file. Prefer omitting
editable server configs from the package and declaring them `preserve`. If a recipe genuinely needs to
seed one, ship a complete upstream-compatible configuration, mark it
`preserve`, and document that future recipe defaults will not replace an
existing operator copy. Use `generated` only when replacing the entire
untouched file is the intended behavior, and `managed` for immutable recipe
content.

## 5. Define Docker runtime and readiness

The image tag is convenient in source; `nesk lock` resolves it to a repository
digest. Runtime commands are argv, never shell text. The explicit root mount
makes instance files visible at `/server`; the root filesystem outside that
mount remains read-only. The instance-contained `.tmp` directory provides a
writable `/tmp` for the Java runtime.

The readiness regex matches Lumi's normal startup log. Validate it against the
exact server release you deploy. A required timeout removes a newly started
container. During update, Luminesk then restores and restarts the previous
instance.

`post-build` checks package staging. `post-install` checks the applied instance.
Both phases currently support only file checks.

## 6. Validate, lock, and plan

Static validation does not contact providers:

```bash
nesk validate --dir ./my-lumi-core --static
```

Resolve providers and the Docker image, then create the lock:

```bash
nesk validate --dir ./my-lumi-core --resolve
nesk lock --dir ./my-lumi-core
```

Review `luminesk.lock`, but do not edit it. Validate package assembly, then
preview the plan with a memory override:

```bash
nesk validate --dir ./my-lumi-core --build
nesk plan --dir ./my-lumi-core \
  --set memory=2g
```

For a recipe with secret inputs, use `--set-file NAME=PATH` just as you would for
an install. Validation reads the secret for temporary rendering without persisting
it.

## 7. Test a local install

Install into a separate empty directory so the recipe source remains clean:

```bash
nesk install ./my-lumi-core \
  --dir ./test-lumi \
  --set memory=2g \
  --dry-run

nesk install ./my-lumi-core \
  --dir ./test-lumi \
  --set memory=2g \
  --yes
```

Then exercise the lifecycle:

```bash
nesk validate --dir ./test-lumi --instance
nesk start --dir ./test-lumi
nesk status --dir ./test-lumi
nesk logs --dir ./test-lumi
nesk validate --dir ./test-lumi --readiness
nesk stop --dir ./test-lumi
# Edit the complete settings.yml generated by Lumi.
nesk start --dir ./test-lumi
```

Inspect the complete server-generated configuration, ownership
behavior, port, memory usage, graceful shutdown, and startup logs. Test with
disposable worlds before treating the recipe as production-ready.

## 8. Test a real recipe update

A local recipe install stores an immutable untracked snapshot; `nesk update`
does not follow later edits in the original local directory. To test the remote
update path, publish the recipe to a review branch and install that branch:

```bash
nesk install example/my-lumi-core@main \
  --dir ./tracked-lumi \
  --yes
```

Change the recipe, bump `package.version`, validate it, and push the branch.
Then review and apply from the tracked test instance:

```bash
nesk outdated --dir ./tracked-lumi
nesk diff --dir ./tracked-lumi
nesk update --dir ./tracked-lumi --dry-run
nesk update --dir ./tracked-lumi --yes
nesk status --dir ./tracked-lumi
nesk logs --dir ./tracked-lumi
```

Verify that edited `settings.yml`, worlds, and plugins were preserved;
untouched managed files changed as planned; readiness passed; and a deliberate
failed readiness condition restores the previous package/runtime. Keep an
independent backup while testing rollback.

## 9. Optional Dockerfile build

Use `[build]` only when a provider artifact and templates cannot produce the
package directly:

```toml
[build]
file = ".luminesk/Dockerfile"
output = "/out"
timeout = 1200
cpu = 2
memory = "2g"
network = false
```

There is no `[permissions]` table. Declaring `[build]` enables the isolated
Docker build; `network = true` explicitly allows its default Docker network.
Review every Dockerfile and keep networking disabled unless the build truly
needs it.

Use the [`luminesk.toml` Reference](/docs/manifest-reference) when extending
the recipe with another provider, platform-specific artifacts, checks, or
mounts.
