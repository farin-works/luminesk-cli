from __future__ import annotations

import io
import tarfile
from pathlib import Path

from luminesk_cli.application.locking import LockService
from luminesk_cli.domain.lockfile import Lockfile, ResolvedSource, RuntimeLock
from luminesk_cli.domain.manifest import parse_manifest
from luminesk_cli.domain.progress import ProgressEvent, observe_progress
from luminesk_cli.infrastructure.build import DeclarativeBuilder
from luminesk_cli.infrastructure.cache import ContentCache
from luminesk_cli.infrastructure.oci import OciImageResolver
from luminesk_cli.infrastructure.package import verify_package


def test_image_only_recipe_builds_metadata_only_package(tmp_path: Path) -> None:
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    manifest = parse_manifest(
        b"""\
manifest_version = 1
[package]
name = "image-only-server"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "bedrock"
[runtime]
image = "example/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["server"]
"""
    )
    lockfile = Lockfile(
        manifest_digest=manifest.digest,
        target="linux/amd64",
        sources={},
        runtime=RuntimeLock(image=manifest.runtime.image),
    )

    events: list[ProgressEvent] = []
    with observe_progress(events.append):
        package = DeclarativeBuilder(ContentCache(tmp_path / "cache")).build(
            manifest,
            lockfile,
            recipe,
            tmp_path / "image-only.lumineskpkg",
        )

    assert package.metadata.files == ()
    assert verify_package(package.path).metadata.files == ()
    assert events == [ProgressEvent("build"), ProgressEvent("verify")]


def test_declarative_build_creates_verified_deterministic_package(
    tmp_path: Path,
) -> None:
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    (recipe / "fixture.jar").write_bytes(b"server")
    (recipe / "server.properties.in").write_text(
        "server-name=${input.name}\nserver-port=${input.port}\n",
        encoding="utf-8",
    )
    manifest = parse_manifest(
        b"""\
manifest_version = 1
[package]
name = "fixture-server"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "bedrock"
[inputs.name]
type = "string"
default = "Luminesk"
[inputs.port]
type = "integer"
default = 19132
[[sources]]
id = "core"
type = "local-file"
target = "server.jar"
[sources.options]
path = "fixture.jar"
[[files]]
source = "server.properties.in"
target = "server.properties"
mode = "generated"
template = true
[[files]]
source = "worlds"
target = "worlds"
mode = "data"
[runtime]
image = "example/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["java", "-jar", "server.jar"]
[[checks]]
id = "core-present"
phase = "post-build"
kind = "file"
path = "server.jar"
"""
    )
    cache = ContentCache(tmp_path / "cache")
    lockfile = LockService(
        cache,
        image_resolver=OciImageResolver(),
    ).create(manifest, recipe, target="linux/amd64")
    builder = DeclarativeBuilder(cache)
    first = builder.build(manifest, lockfile, recipe, tmp_path / "one.lumineskpkg")
    second = builder.build(manifest, lockfile, recipe, tmp_path / "two.lumineskpkg")

    assert first.digest == second.digest
    assert verify_package(first.path).metadata.name == "fixture-server"
    file_metadata = {item.path: item for item in first.metadata.files}
    assert file_metadata["server.properties"].ownership == "generated"
    assert file_metadata["worlds"].ownership == "data"


def test_template_rejects_environment_interpolation(tmp_path: Path) -> None:
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    (recipe / "fixture.jar").write_bytes(b"server")
    (recipe / "template.txt").write_text("${HOME}", encoding="utf-8")
    manifest = parse_manifest(
        b"""\
manifest_version = 1
[package]
name = "fixture-server"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "bedrock"
[[sources]]
id = "core"
type = "local-file"
target = "server.jar"
[sources.options]
path = "fixture.jar"
[[files]]
source = "template.txt"
target = "config.txt"
template = true
[runtime]
image = "example/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["./server.jar"]
"""
    )
    cache = ContentCache(tmp_path / "cache")
    lockfile = LockService(cache, image_resolver=OciImageResolver()).create(
        manifest, recipe, target="linux/amd64"
    )
    package = DeclarativeBuilder(cache).build(
        manifest, lockfile, recipe, tmp_path / "package.lumineskpkg"
    )

    import zipfile

    with zipfile.ZipFile(package.path) as archive:
        assert archive.read("payload/config.txt") == b"${HOME}"


def test_github_source_materializes_only_declared_subtree(tmp_path: Path) -> None:
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    manifest_bytes = b"""\
manifest_version = 1
[package]
name = "github-source-fixture"
version = "2.0.0"
kind = "template"
game = "minecraft"
edition = "cross-platform"
[[sources]]
id = "assets"
type = "github-source"
target = "assets"
extract = true
[sources.options]
repository = "owner/project"
ref = "main"
path = "server-assets"
[runtime]
image = "example/server:2"
command = ["./server"]
"""
    (recipe / "luminesk.toml").write_bytes(manifest_bytes)
    manifest = parse_manifest(manifest_bytes)
    source_archive = tmp_path / "source.tar.gz"
    with tarfile.open(source_archive, "w:gz") as archive:
        for path, content in (
            ("owner-project-commit/server-assets/config.yml", b"runtime: true\n"),
            ("owner-project-commit/src/unrelated.cpp", b"not runtime\n"),
            ("owner-project-commit/tests/unrelated.py", b"not runtime\n"),
        ):
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    cache = ContentCache(tmp_path / "cache")
    from luminesk_cli.infrastructure.cache import digest_file

    digest, size = digest_file(source_archive)
    cache.store(source_archive, digest)
    lockfile = Lockfile(
        manifest_digest=manifest.digest,
        target="linux/amd64",
        sources={
            "assets": ResolvedSource(
                type="github-source",
                version="main",
                source_revision="a" * 40,
                url="https://api.github.com/repos/owner/project/tarball/" + "a" * 40,
                size=size,
                digest=digest,
                target="assets",
                media_type="application/gzip",
            )
        },
        runtime=RuntimeLock(image=f"example/server@sha256:{'b' * 64}"),
    )

    package = DeclarativeBuilder(cache).build(
        manifest,
        lockfile,
        recipe,
        tmp_path / "github-source.lumineskpkg",
    )

    import zipfile

    with zipfile.ZipFile(package.path) as archive:
        names = set(archive.namelist())
        assert "payload/assets/config.yml" in names
        assert not any("src" in name or "tests" in name for name in names)
