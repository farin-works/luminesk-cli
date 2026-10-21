from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from luminesk_cli.domain.errors import ValidationError
from luminesk_cli.domain.lockfile import (
    LOCKFILE_NAME,
    BuildLock,
    Lockfile,
    RecipeLock,
    ResolvedSource,
    RuntimeLock,
    load_lockfile,
    parse_lockfile,
    write_lockfile,
)


def make_lockfile() -> Lockfile:
    return Lockfile(
        manifest_digest=f"sha256:{'a' * 64}",
        target="linux/amd64",
        sources={
            "core": ResolvedSource(
                type="http",
                version="2.0.0",
                source_revision="2.0.0",
                url="https://example.org/server.jar",
                size=42,
                digest=f"sha256:{'b' * 64}",
                target="server.jar",
            )
        },
        runtime=RuntimeLock(image=f"example:1@sha256:{'c' * 64}"),
    )


def test_lockfile_round_trip_is_canonical(tmp_path: Path) -> None:
    path = tmp_path / LOCKFILE_NAME
    original = make_lockfile()

    write_lockfile(path, original)
    loaded = load_lockfile(path)

    assert loaded == original
    assert path.read_bytes() == original.to_bytes()
    assert original.to_bytes().endswith(b"\n")


def test_lockfile_rejects_unpinned_image() -> None:
    content = (
        make_lockfile()
        .to_bytes()
        .replace(f"example:1@sha256:{'c' * 64}".encode(), b"example:latest")
    )

    with pytest.raises(ValidationError, match="pinned"):
        parse_lockfile(content)


def test_lockfile_rejects_digest_with_trailing_tag() -> None:
    pinned = f"example:1@sha256:{'c' * 64}"
    content = (
        make_lockfile()
        .to_bytes()
        .replace(
            pinned.encode(),
            f"{pinned}:latest".encode(),
        )
    )

    with pytest.raises(ValidationError, match="pinned"):
        parse_lockfile(content)


def test_lockfile_rejects_credentials() -> None:
    content = (
        make_lockfile()
        .to_bytes()
        .replace(b"https://example.org", b"https://user:secret@example.org")
    )

    with pytest.raises(ValidationError, match="credentials"):
        parse_lockfile(content)


def test_lockfile_round_trips_pinned_build_images() -> None:
    original = make_lockfile()
    original = Lockfile(
        manifest_digest=original.manifest_digest,
        target=original.target,
        sources=original.sources,
        runtime=original.runtime,
        build=BuildLock(images={"golang:1.26": f"golang@sha256:{'d' * 64}"}),
    )

    assert parse_lockfile(original.to_bytes()) == original


@pytest.mark.parametrize("kind", ["database", "github", "local"])
def test_lockfile_round_trips_complete_recipe_origin(kind: str) -> None:
    original = make_lockfile()
    if kind == "database":
        recipe = RecipeLock(
            kind="database",
            source="github:task-v1/luminesk-database",
            revision="d" * 40,
            entry="paper",
            path="database/paper",
            tracking=True,
            version="1.0.1",
            manifest_digest=f"sha256:{'e' * 64}",
            template_digest=f"sha256:{'f' * 64}",
        )
    elif kind == "github":
        recipe = RecipeLock(
            kind="github",
            source="github:owner/repository",
            revision="d" * 40,
            ref="main",
            tracking=True,
            version="1.0.1",
            manifest_digest=f"sha256:{'e' * 64}",
        )
    else:
        recipe = RecipeLock(
            kind="local",
            source="local",
            revision=f"sha256:{'d' * 64}",
            tracking=False,
            version="1.0.1",
            manifest_digest=f"sha256:{'e' * 64}",
        )
    complete = Lockfile(
        manifest_digest=original.manifest_digest,
        target=original.target,
        sources=original.sources,
        runtime=original.runtime,
        recipe=recipe,
    )

    assert parse_lockfile(complete.to_bytes()) == complete
    assert set(complete.to_dict()["recipe"]) == {
        "kind",
        "source",
        "revision",
        "entry",
        "path",
        "ref",
        "tracking",
        "version",
        "manifestDigest",
        "templateDigest",
    }


def test_recipe_lock_rejects_absolute_local_origin() -> None:
    with pytest.raises(ValidationError, match="local"):
        RecipeLock(
            kind="local",
            source="/private/recipe",
            revision=f"sha256:{'d' * 64}",
            tracking=False,
            version="1.0.0",
            manifest_digest=f"sha256:{'e' * 64}",
        )


@pytest.mark.parametrize("owner", ["farin-works", "task-v1"])
def test_database_lock_preserves_official_origin_after_rename(owner: str) -> None:
    recipe = RecipeLock(
        kind="database",
        source=f"github:{owner}/luminesk-database",
        revision="d" * 40,
        entry="paper",
        path="database/paper",
        tracking=True,
        version="1.0.1",
        manifest_digest=f"sha256:{'e' * 64}",
    )
    original = replace(make_lockfile(), recipe=recipe)

    assert parse_lockfile(original.to_bytes()).to_bytes() == original.to_bytes()
    with pytest.raises(ValidationError, match="not official"):
        replace(recipe, source="github:untrusted/luminesk-database")
