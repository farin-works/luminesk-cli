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
