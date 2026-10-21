---
sidebar_position: 3
---

# Installation

Luminesk supports a one-line installer, Python tool installation, and
self-contained release bundles. Choose one installation method and keep using
the same method for updates; do not layer multiple managers over one `nesk`
executable.

## Requirements

- **Python 3.13 or newer** for `uv tool install`; release bundles include their
  own Python runtime.
- **Docker Engine or Docker Desktop** for running servers, resolving mutable
  image tags, and recipes that declare a Dockerfile build.
- **HTTPS access** while refreshing the catalog or resolving remote artifacts.

Git is not required for normal use. Luminesk acquires GitHub recipes through
the GitHub API, limits what it downloads, and pins the selected commit.

## Install with one command

The one-line installer downloads the self-contained onedir ZIP bundle, so
Python is not required.

On Linux or macOS:

```bash
curl -fsSL https://nesk.farin.cc/sh | sh -s -- --yes
```

On Windows, open PowerShell and run:

```powershell
irm https://nesk.farin.cc/ps1 | iex
```

The installer:

- detects AMD64 or ARM64 on Linux/macOS, and AMD64 (x64) on Windows;
- selects the matching asset from the latest GitHub release;
- requires SHA-256 from GitHub release metadata (Windows) or the release
  `SHA256SUMS` (Linux/macOS), and verifies the ZIP before extracting any files;
- checks the complete bundle inventory, file hashes, imports (including
  `prompt_toolkit`), version, and help before activating a new version;
- refuses to overwrite a `nesk` owned by uv, pipx, or a manual installation.

In the Unix command, `--yes` is an installer option: piping occupies standard
input, so the installer cannot ask its normal terminal question. This option
approves only installing the Luminesk executable. It does not accept the
Minecraft EULA, provide a recipe input, or approve a later `nesk install` or
`nesk update` plan. The PowerShell command asks before writing.

A normal user installation needs no administrator rights or installed Python.
On Linux/macOS, `~/.local/bin/nesk` is a symlink into
`~/.local/share/luminesk/app/versions/<version>-<installation-id>/nesk/`.
On Windows, `~/.local/bin/nesk.cmd` forwards to the equivalent version directory,
using a relative path so Unicode home directories work. The launcher forwards
arguments and standard streams, and returns the application's exit code.
The executable and its `_internal` directory always stay together; `_internal`
is never added to `PATH`.

A root Unix installation uses `/usr/local/bin/nesk` and
`/usr/local/share/luminesk/app`. An elevated Windows installation uses
`$env:ProgramFiles\luminesk\nesk.cmd` and an adjacent `app` directory, with
metadata under `$env:ProgramData\luminesk`. Run updates at the same privilege
level as the original installation. An installation owned by another manager,
or a different `nesk` earlier in `PATH`, is a conflict and is not overwritten.

Windows adds the launcher directory to the corresponding persistent PATH and
the current PowerShell environment. Unix configures user shell profiles if
`~/.local/bin` is missing; open a new terminal after the first installation.
A piped shell child cannot change the environment of its parent shell.
Linux/macOS installation requires `curl`, `unzip`, standard POSIX utilities,
and `sha256sum` or `shasum`. Docker is only needed to operate servers.

Run the same one-line command later to check for and install an update. To
remove an installation owned by the script, use:

```bash
curl -fsSL https://nesk.farin.cc/sh | sh -s -- --delete --yes
```

```powershell
& ([scriptblock]::Create((irm https://nesk.farin.cc/ps1))) "--delete"
```

These commands execute a remote script over HTTPS. You can review the complete
[Linux/macOS installer](https://nesk.farin.cc/sh) or
[Windows installer](https://nesk.farin.cc/ps1) before running it.

## Install with uv

With Python 3.13+ and uv installed:

```bash
uv tool install luminesk-cli
nesk --version
nesk doctor
nesk catalog update
```

Upgrade the tool with:

```bash
uv tool upgrade luminesk-cli
```

If the installation succeeds but the command is not found, run:

```bash
uv tool update-shell
```

Then open a new terminal. You can also follow uv's printed instruction and add
its tool binary directory to `PATH` manually.

## Install with pipx

With Python 3.13+ and pipx installed:

```bash
pipx install luminesk-cli
nesk --version
nesk doctor
```

Upgrade the isolated environment with:

```bash
pipx upgrade luminesk-cli
```

If `nesk` is not found, run `pipx ensurepath`, open a new terminal, and retry.
Use either uv or pipx to own the tool installation; do not layer both managers
over the same executable path.

## Prebuilt release bundles

Release bundles do not require a system Python. Each tagged release publishes
an onedir ZIP for these targets:

- `luminesk-linux-amd64.zip`
- `luminesk-linux-arm64.zip`
- `luminesk-macos-amd64.zip`
- `luminesk-macos-arm64.zip`
- `luminesk-windows-amd64.zip`

Download the matching archive and `SHA256SUMS` from
[GitHub Releases](https://github.com/farin-works/luminesk-cli/releases/latest),
verify its checksum, and extract it. The archive contains a `nesk` directory;
keep its executable and adjacent `_internal` directory together.

- On Linux or macOS, add the extracted `nesk` directory to your shell's `PATH`.
- On Windows, add the extracted `nesk` directory to the user `Path` in System
  Properties, then open a new PowerShell window.

The official installer uses these same ZIPs. Releases no longer publish the
standalone PyInstaller onefile executables. Wheel and source distributions
remain available for uv and pipx.

## Updates, migration, and recovery

Each installation extracts into a private staging directory, checks the
inventory and every regular file's SHA-256, and runs smoke tests. POSIX links
are created only after regular files and directories; their targets must resolve
inside the bundle. Windows rejects archive links and special files. Downloads,
entry counts, inventory size, and expanded file sizes are bounded.

A successful attempt moves the complete bundle to a new version directory.
Unix activates it by atomically replacing the `nesk` symlink. Windows atomically
replaces the CMD launcher and restores the previous launcher and metadata if
activation fails. Download and validation failures leave the current command
untouched. Old versions are retained, including on reinstall, to avoid disturbing
running processes. Do not manually replace only the executable.

Existing onefile installations are migrated automatically when their
`.metadata_one_line` declares `installed_via=one-line-script` and the exact
expected binary path. The legacy executable is retained in the new version
directory. On Unix its inode is retained by a hardlink, so `bin` and `app`
must be on the same filesystem for legacy migration. No prior uninstall is required. Metadata for another path does not
grant ownership of a uv/pipx executable.

Only one installer may operate on an installation at a time. Windows uses an
OS-held file lock that is released on process termination. Unix uses an atomic
lock directory. If a Unix installer is killed without cleanup or the machine
loses power, the old command or the fully checked new command remains active;
check that no installer is still running, then remove only
`app/install.lock` with `rmdir` and rerun the same installer. Never remove an
active installer's lock. Interrupted staging directories and retained versions
may consume disk space; the installer does not prune them during updates.

`--delete` removes installer-owned program versions, the entry point, and its
metadata. Instances, worlds, plugins, configuration, and content caches are not
removed. Shared binary directories and unrelated files in them are preserved.
If a version directory contains files outside its recorded installation inventory,
uninstall stops before removing the command; move those user files elsewhere first.
Close running `nesk` processes before uninstalling. Windows checks for locked
application files; Unix requires `lsof` and refuses removal if files are in use
or the inspection cannot complete. PATH entries and shell profile lines are retained
because these directories may also contain other tools.

## Platform notes

### Linux

Docker access is user-specific. `docker version` must succeed as the same user
that runs `nesk`. If Docker reports a permission error, follow your
distribution's Docker post-installation guidance or run Docker rootless.

Typical user-local binary directories include `~/.local/bin`. Add one to the
current shell, for example:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Persist the equivalent setting in your shell profile after confirming it.

### macOS

Start Docker Desktop before using image, build, or runtime operations. For uv,
restart the terminal after `uv tool update-shell`. For a bundle, add the
directory containing `nesk` to the shell `PATH` and keep `_internal` beside it.

### Windows

Windows requires AMD64 (x64). Use Python 3.13+ with uv, or download the
`windows-amd64` bundle. After changing the user `Path`, start a new
PowerShell session and check resolution with:

```powershell
Get-Command nesk
nesk --version
```

Docker Desktop must be running and configured for Linux containers.

## Verify

```bash
nesk --version
nesk --help
nesk doctor
docker version
nesk catalog update
```

`doctor` checks both the Docker CLI and daemon connectivity; `docker version`
prints their detailed versions. The one-line installers stop if release
metadata, the matching asset digest, or a SHA-256 tool is unavailable; they
never install an unverified download. If a release bundle fails before `--version`,
compare its checksum with `SHA256SUMS`, remove the extracted copy, and extract
the entire archive again. See [Troubleshooting](/docs/troubleshooting) for PATH,
Docker, and damaged-download diagnostics.
