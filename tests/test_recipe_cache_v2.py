from __future__ import annotations

import json
from pathlib import Path

import pytest

from luminesk_cli.application.locking import LockService
from luminesk_cli.cli.entry import main
from luminesk_cli.domain.errors import SecurityError
from luminesk_cli.domain.lockfile import BuildLock, Lockfile, RecipeLock, RuntimeLock
from luminesk_cli.domain.manifest import parse_manifest
from luminesk_cli.infrastructure.cache import ContentCache
from luminesk_cli.infrastructure.oci import OciImageResolver
from luminesk_cli.infrastructure.recipe_cache import RecipeCache, github_locator
from luminesk_cli.infrastructure.recipe_snapshot import create_recipe_snapshot

REVISION = "a" * 40
PINNED_IMAGE = f"example/server@sha256:{'b' * 64}"


pytestmark = pytest.mark.usefixtures("docker_engine")


def _local_source_snapshot(root: Path):
    root.mkdir()
    (root / "server.bin.in").write_bytes(b"offline server")
    manifest_bytes = f'''\
manifest_version = 1
[package]
name = "offline-fixture"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "bedrock"
[[sources]]
id = "core"
type = "local-file"
target = "server.bin"
[sources.options]
path = "server.bin.in"
[runtime]
image = "{PINNED_IMAGE}"
command = ["./server.bin"]
'''.encode()
    (root / "luminesk.toml").write_bytes(manifest_bytes)
    manifest = parse_manifest(manifest_bytes)
    return create_recipe_snapshot(
        root,
        manifest,
        kind="github",
        source="github:owner/repository",
        revision=REVISION,
        ref="main",
        tracking=True,
    )


def test_frozen_github_install_uses_only_verified_cache(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from luminesk_cli.cli.commands import install as install_command

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    snapshot = _local_source_snapshot(tmp_path / "recipe")
    calls = 0

    def acquire(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        return snapshot

    monkeypatch.setattr(install_command, "acquire_github_recipe", acquire)
    first = tmp_path / "first"
    assert main(["i", "owner/repository", "--dir", str(first), "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert calls == 1

    def reject_network(*args, **kwargs):
        del args, kwargs
        raise AssertionError("frozen install attempted recipe acquisition")

    monkeypatch.setattr(install_command, "acquire_github_recipe", reject_network)
    assert (
        main(
            [
                "update",
                "--dir",
                str(first),
                "--frozen",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "second-config-home"))
    second = tmp_path / "second"
    assert (
        main(
            [
                "i",
                "owner/repository",
                "--dir",
                str(second),
                "--frozen",
                "--yes",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert (second / "server.bin").read_bytes() == b"offline server"


def test_recipe_cache_detects_tampered_snapshot(tmp_path: Path) -> None:
    snapshot = _local_source_snapshot(tmp_path / "recipe")
    content_cache = ContentCache(tmp_path / "content")
    lockfile = LockService(
        content_cache,
        image_resolver=OciImageResolver(),
    ).create(
        snapshot.manifest,
        snapshot.root,
        recipe_origin=snapshot.origin,
        target="linux/amd64",
    )
    recipes = RecipeCache(tmp_path / "recipes")
    recipes.store(
        snapshot,
        lockfile,
        locator=github_locator("github:owner/repository", None),
    )
    cached = recipes.load_locator(
        github_locator("github:owner/repository", None),
        "linux/amd64",
    )
    (cached.snapshot.root / "luminesk.toml").write_bytes(b"tampered")

    with pytest.raises(SecurityError, match="files"):
        recipes.load_locator(
            github_locator("github:owner/repository", None),
            "linux/amd64",
        )


def test_recipe_cache_keeps_bounded_build_context_outside_instance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "upstream"
    (root / ".luminesk").mkdir(parents=True)
    (root / "src").mkdir()
    (root / ".luminesk/Dockerfile").write_text(
        "FROM scratch\nCOPY src /out\n",
        encoding="utf-8",
    )
    (root / "src/main.c").write_text("int main(void) { return 0; }\n")
    manifest_bytes = f'''\
manifest_version = 1
[package]
name = "build-fixture"
version = "1.0.0"
kind = "core"
game = "minecraft"
edition = "java"
[build]
file = ".luminesk/Dockerfile"
output = "/out"
[runtime]
image = "{PINNED_IMAGE}"
command = ["./server"]
'''.encode()
    (root / "luminesk.toml").write_bytes(manifest_bytes)
    manifest = parse_manifest(manifest_bytes)
    snapshot = create_recipe_snapshot(
        root,
        manifest,
        kind="github",
        source="github:owner/build",
        revision=REVISION,
        ref="v1.0.0",
        tracking=False,
    )
    lockfile = Lockfile(
        manifest_digest=manifest.digest,
        target="linux/amd64",
        sources={},
        runtime=RuntimeLock(image=PINNED_IMAGE),
        build=BuildLock(images={}),
        recipe=RecipeLock(
            kind="github",
            source=snapshot.origin.source,
            revision=snapshot.origin.revision,
            ref=snapshot.origin.ref,
            tracking=False,
            version=snapshot.origin.version,
            manifest_digest=snapshot.origin.manifest_digest,
            template_digest=snapshot.origin.template_digest,
        ),
    )

    cached = RecipeCache(tmp_path / "recipes").store(snapshot, lockfile)

    assert (cached.snapshot.root / "src/main.c").is_file()
    assert "src/main.c" not in {entry.path for entry in cached.snapshot.entries}
