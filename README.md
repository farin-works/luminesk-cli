<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://github.com/farin-works/luminesk-cli/raw/refs/heads/main/docs/static/img/logo-with-cli.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://github.com/farin-works/luminesk-cli/raw/refs/heads/main/docs/static/img/logo-with-cli-dark.svg">
    <img src="https://github.com/farin-works/luminesk-cli/raw/refs/heads/main/docs/static/img/logo-with-cli-dark.svg" width="500" alt="Luminesk-CLI">
  </picture>

  <p><strong>A reproducible composer for Minecraft Java and Bedrock server instances</strong></p>

  <p>
    <a href="https://github.com/farin-works/luminesk-cli/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/farin-works/luminesk-cli/ci.yml?branch=main&label=CI"></a>
    <a href="https://github.com/farin-works/luminesk-cli/releases/latest"><img alt="GitHub Release" src="https://img.shields.io/github/v/release/farin-works/luminesk-cli"></a>
    <a href="https://pypi.org/project/luminesk-cli/"><img alt="PyPI Version" src="https://img.shields.io/pypi/v/luminesk-cli"></a>
    <a href="https://github.com/farin-works/luminesk-cli/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/github/license/farin-works/luminesk-cli"></a>
    <a href="https://nesk.farin.cc"><img alt="Docs" src="https://img.shields.io/badge/docs-online-0ea5e9"></a>
  </p>
</div>

Luminesk-CLI is maintained by [Farin Works](https://github.com/farin-works).
Documentation and installers are available at
[nesk.farin.cc](https://nesk.farin.cc).

## What Luminesk does

Luminesk-CLI (`nesk`) installs and operates Minecraft Java and Bedrock servers
from reviewable recipes. One server lives in one directory. Docker is the only
runtime, so the same lifecycle works on Linux, macOS, and Windows without
installing a Java, PHP, or other server runtime directly on the host.

A recipe tells Luminesk where the server comes from, which inputs it needs,
which ports and files it uses, how to decide that it is ready, and which data
must survive an update. Luminesk resolves exact artifact hashes and Docker
image digests, shows a plan, and applies it transactionally.

## Your first server

You need Docker Engine or Docker Desktop. Git is not required for normal use.
On Windows, use Docker Desktop with Linux containers (WSL2). Recipe platforms
refer to the Docker Engine's OS/architecture; Lumi's `runtime_uid` and
`runtime_gid` select a Linux container identity, not a Windows account.
The quickest installation uses the self-contained onedir release bundle and
does not require Python.

Linux or macOS:

```bash
curl -fsSL https://nesk.farin.cc/sh | sh -s -- --yes
```

Windows PowerShell:

```powershell
irm https://nesk.farin.cc/ps1 | iex
```

The installer detects the operating system and CPU, downloads the latest
release, and verifies its SHA-256 digest before installing it. Run the same
command again to update. The Unix `--yes` belongs to the installer and does not
accept the Minecraft EULA or approve later `nesk` operations. If you prefer a
Python-managed installation, Python 3.13+ is required:

```bash
uv tool install luminesk-cli
```

Then check the environment and install a recipe:

```bash
nesk doctor
nesk catalog update
nesk search --type core
nesk info lumi
nesk install lumi --dir ./servers/example
```

`nesk info lumi` lists every input before installation. In an interactive
terminal, `install` opens a wizard: press Enter to keep a default and answer
any required recipe questions. It then shows the exact recipe, downloads,
Docker image, capabilities, and file changes before asking for confirmation.

`lumi` is a Bedrock example. Use `nesk search --edition java` to find Java
recipes. A remote install needs a new, empty destination directory. With pipx,
use `pipx install luminesk-cli`. ZIP bundles for Linux, macOS, and Windows are
also available on the
[GitHub Releases](https://github.com/farin-works/luminesk-cli/releases/latest) page.

## Start, configure, and use the console

```bash
nesk start --dir ./servers/example
nesk status --dir ./servers/example
nesk attach --dir ./servers/example
```

`attach` opens a full-screen console with recent history and live output. Type
a server command and press Enter. Use `Ctrl+D` to detach while leaving the
server running, `Ctrl+C` to stop it gracefully, or `Ctrl+K` to kill it.
`Ctrl+L` clears the local view, `PageUp`/`PageDown` scroll, and `End` follows
new output. Server ANSI colors are rendered safely; `NO_COLOR` disables color.

To start and open the same console in one command:

```bash
nesk start --dir ./servers/lumi --attach
```

This waits for readiness, then attaches. If the server is already running,
it opens that container's console without restarting it. Inside the instance,
use `nesk start --attach`. A real terminal is required; `--attach` rejects
`--json` and `--non-interactive` before starting the server.

Editable server configs are normally created by the server on its first start.
Official recipes do not seed partial `server.properties`-style files. Stop the
server, edit the generated config inside the instance directory, then start it
again. EULA files are the deliberate exception for recipes that require an
explicit acceptance input.

When your shell is already inside the instance, `--dir` may be omitted. From
anywhere else, pass it explicitly so there is no ambiguity about which server
the command targets.

```bash
nesk logs --dir ./servers/example --tail 500 --since 10m --timestamps
nesk stop --dir ./servers/example
nesk list
```

## Review an update before applying it

```bash
nesk outdated --dir ./servers/example
nesk diff --dir ./servers/example
nesk update --dir ./servers/example --dry-run
nesk update --dir ./servers/example --yes
```

Luminesk refuses to silently overwrite a locally changed managed file. An
update of a running server stops it, applies the reviewed package, starts it,
waits for readiness, and attempts to restore the previous instance if a
required step fails. Transaction rollback is not a replacement for an
independent backup of production worlds.

## Automation

Interactive defaults are intentionally disabled for scripts. Supply inputs and
trust approval explicitly:

```bash
nesk install lumi --dir ./servers/example \
  --dry-run --json --non-interactive
nesk install lumi --dir ./servers/example \
  --yes --json --non-interactive
```

Handled failures use stable exit codes and a JSON `error` object. Secret inputs
must use `--set-file`; Luminesk does not persist them or place them in runtime
arguments.

## What is stored in an instance?

- `luminesk.toml` — the reviewed recipe;
- `luminesk.lock` — exact source and image identities;
- `.luminesk_cli/` — ownership, transaction, backup, recipe, and runtime state;
- server-owned files such as worlds, plugins, logs, and generated configs.

Do not hand-edit the lock or `.luminesk_cli/`. Use `nesk diff`,
`nesk validate --instance`, and `nesk recover` when state needs inspection or
recovery.

## Custom recipes and migration

Recipe authors can start with:

```bash
nesk init --dir ./recipe --name example-server
# Replace the placeholder source in luminesk.toml.
nesk validate --dir ./recipe --static
nesk lock --dir ./recipe
nesk plan --dir ./recipe
```

Read the [documentation](https://nesk.farin.cc),
[Quick Start](https://nesk.farin.cc/docs/quick-start), and
[Command Reference](https://nesk.farin.cc/docs/command-reference).
Luminesk 1.x instances require a
[side-by-side migration](https://nesk.farin.cc/docs/migrating-to-2.0);
their control state and instance formats are not interchangeable.

## Development

```bash
uv sync --locked --extra dev
uv run python scripts/format.py --fix
uv run ty check .
uv run pytest
```

See
[CONTRIBUTING.md](https://github.com/farin-works/luminesk-cli/blob/main/CONTRIBUTING.md).
Recipe trust, tested backups, and
workload-specific validation remain the operator's responsibility.

## License

GPL-3.0-or-later. See
[LICENSE](https://github.com/farin-works/luminesk-cli/blob/main/LICENSE).
