---
sidebar_position: 12
---

# Development and Contributing

Repository development uses uv and, unlike end-user recipe installation,
naturally requires Git for source control.

```bash
git clone https://github.com/task-v1/luminesk-cli
cd luminesk-cli
uv sync --locked --extra dev
```

Run the complete local gate after code changes:

```bash
uv run python scripts/format.py --fix
uv run ty check .
uv run pytest
```

ty checks application code, tests, and scripts using the minimum Python version
declared in `pyproject.toml`. Its platform target is `all` so local checks and CI
analyze the same code, including Unix-only release scripts.

Release-sensitive changes should also run:

```bash
uv run python scripts/security_gate.py
uv run python scripts/check_cold_path.py
uv build
uv run python scripts/verify_wheel.py dist/*.whl
```

Use conventional commits. Update user-facing docs, regression tests, and stable
JSON behavior in the same change as any contract modification. CI separately
checks formatting, typing, a cross-platform test matrix, branch coverage, wheel
installation, security policy, documentation, and a real Docker lifecycle.

## Frozen startup comparison

`scripts/benchmark_bundles.py` compares a historical onefile build, the onedir
executable, and the installed launcher. It interleaves 25 fresh processes per
variant and reports the median and nearest-rank p95 without a timing gate.
User state is isolated; `doctor` measures the missing-Docker error path and
never connects to a real daemon. Use an existing historical onefile artifact
or build one locally for comparison; it is not a published release asset.

```bash
uv run python scripts/benchmark_bundles.py --onefile PATH_TO_ONEFILE --onedir PATH_TO_ONEDIR_EXECUTABLE --launcher PATH_TO_LAUNCHER --samples 25 --output report/startup.json
```

Measured on 2026-10-12: Windows 11 AMD64 (build 26300), Intel Family 6 Model
141, Python 3.14.8, PyInstaller 6.21.0, Luminesk 2.0.2. The baseline uses the
previous release's onefile build options; onedir adds explicit prompt-toolkit
collection and the private import probe. Numbers below are milliseconds,
median / p95, with a warm OS file cache.

| Command | Onefile | Onedir | CMD launcher |
| --- | ---: | ---: | ---: |
| `--version` | 1399.8 / 1462.7 | 130.4 / 159.1 | 181.8 / 209.8 |
| `--help` | 1427.7 / 1586.8 | 167.5 / 208.2 | 216.6 / 268.5 |
| `list` | 1834.7 / 1975.9 | 292.7 / 338.5 | 340.1 / 399.2 |
| `doctor` | 1691.4 / 1814.6 | 271.3 / 326.0 | 312.3 / 338.6 |

The `--version` median through the launcher was about 7.7 times faster than
onefile on this host. This is not a universal speed guarantee. A true cold-cache
run after reboot was not measured; the script labels its first observation
separately and never presents it as cold-cache data. Linux, macOS, and ARM64
performance measurements remain platform-specific follow-up work.
