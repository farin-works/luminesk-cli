from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def docker_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI orchestration tests use a deterministic engine, never the user's daemon."""
    from luminesk_cli.infrastructure.oci import OciImageResolver

    monkeypatch.setattr(OciImageResolver, "target_platform", lambda self: "linux/amd64")


@pytest.fixture(autouse=True)
def isolate_cli_user_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep platformdirs-backed CLI state isolated on every operating system."""

    from luminesk_cli.cli.commands import common

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))

    def cache_directory(application: str) -> str:
        return str(Path(os.environ["XDG_CACHE_HOME"]) / application)

    def config_directory(application: str) -> str:
        return str(Path(os.environ["XDG_CONFIG_HOME"]) / application)

    monkeypatch.setattr(common, "user_cache_dir", cache_directory)
    monkeypatch.setattr(common, "user_config_dir", config_directory)
