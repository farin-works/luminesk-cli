from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from luminesk_cli.domain.errors import SecurityError, ValidationError
from luminesk_cli.domain.manifest import Build
from luminesk_cli.infrastructure import build as build_module
from luminesk_cli.infrastructure.build import DockerfileBuilder
from luminesk_cli.infrastructure.dockerfile import (
    dockerfile_base_images,
    rewrite_dockerfile,
)
from luminesk_cli.infrastructure.oci import OciImageResolver


def test_dockerfile_images_ignore_named_stages_and_are_rewritten(
    tmp_path: Path,
) -> None:
    path = tmp_path / "Dockerfile"
    path.write_text(
        "FROM --platform=linux/amd64 golang:1.26 AS build\n"
        "RUN go build\n"
        "FROM build AS copied\n"
        "FROM alpine:3.23\n"
        "COPY --from=copied /out /out\n",
        encoding="utf-8",
    )

    assert dockerfile_base_images(path) == ("golang:1.26", "alpine:3.23")
    rewritten = rewrite_dockerfile(
        path.read_text(encoding="utf-8"),
        {
            "golang:1.26": f"golang@sha256:{'a' * 64}",
            "alpine:3.23": f"alpine@sha256:{'b' * 64}",
        },
    )

    assert "golang:1.26" not in rewritten
    assert "alpine:3.23" not in rewritten
    assert "FROM build AS copied" in rewritten


def test_dockerfile_dynamic_base_image_is_forbidden(tmp_path: Path) -> None:
    path = tmp_path / "Dockerfile"
    path.write_text("ARG BASE\nFROM ${BASE}\n", encoding="utf-8")

    with pytest.raises(SecurityError, match="dynamic"):
        dockerfile_base_images(path)


def test_dockerfile_rewrite_rejects_stale_build_lock() -> None:
    with pytest.raises(ValidationError, match="no longer present"):
        rewrite_dockerfile(
            "FROM alpine:3.23\n",
            {"debian:13": f"debian@sha256:{'a' * 64}"},
        )


def test_dockerfile_syntax_frontend_requires_digest_pin(tmp_path: Path) -> None:
    path = tmp_path / "Dockerfile"
    path.write_text(
        "# syntax=docker/dockerfile:1\nFROM alpine:3.23\n",
        encoding="utf-8",
    )

    with pytest.raises(SecurityError, match="pinned by sha256"):
        dockerfile_base_images(path)
    with pytest.raises(SecurityError, match="pinned by sha256"):
        rewrite_dockerfile(path.read_text(encoding="utf-8"), {})

    path.write_text(
        f"# syntax=docker/dockerfile:1@sha256:{'c' * 64}\nFROM alpine:3.23\n",
        encoding="utf-8",
    )
    assert dockerfile_base_images(path) == ("alpine:3.23",)


def test_dockerfile_build_and_output_container_use_locked_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    (recipe / "Dockerfile").write_text("FROM alpine:3.23\n", encoding="utf-8")
    targets: list[str] = []
    monkeypatch.setattr(
        OciImageResolver, "require_target", lambda self, target: targets.append(target)
    )
    calls: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        timeout = kwargs["timeout"]
        assert isinstance(timeout, int | float) and timeout > 0
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(build_module, "run_docker", run)
    monkeypatch.setattr(build_module.subprocess, "run", run)
    DockerfileBuilder().build(
        recipe,
        Build(file="Dockerfile", output="/out"),
        tmp_path / "out",
        {"alpine:3.23": f"alpine@sha256:{'a' * 64}"},
        target="linux/arm64",
    )
    assert targets == ["linux/arm64"]
    for operation in ("build", "create"):
        argv = next(call for call in calls if call[1] == operation)
        assert argv[argv.index("--platform") + 1] == "linux/arm64"
    assert "--pull=never" in calls[1]
