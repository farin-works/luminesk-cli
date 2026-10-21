from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from luminesk_cli.cli.entry import main
from luminesk_cli.domain.catalog import parse_catalog_index
from luminesk_cli.domain.errors import ResolutionError
from luminesk_cli.domain.manifest import parse_manifest
from luminesk_cli.domain.primitives import sha256_digest
from luminesk_cli.infrastructure.cache import ContentCache
from luminesk_cli.infrastructure.catalog import CatalogStore
from luminesk_cli.infrastructure.recipe import (
    acquire_github_recipe,
    normalize_git_source,
)
from luminesk_cli.infrastructure.recipe_snapshot import create_recipe_snapshot
from luminesk_cli.infrastructure.template import read_template_tree

REVISION = "a" * 40
PINNED_IMAGE = f"example/server@sha256:{'b' * 64}"


pytestmark = pytest.mark.usefixtures("docker_engine")


def _manifest(*, build: bool = False, template: bool = True) -> bytes:
    template_line = 'template = "template"\n' if template else ""
    build_section = (
        '[build]\nfile = ".luminesk/Dockerfile"\noutput = "/out"\n' if build else ""
    )
    return f'''\
manifest_version = 1
{template_line}[package]
name = "fixture"
version = "2.0.0"
display_name = "Fixture"
kind = "core"
game = "minecraft"
edition = "java"
summary = "Fixture server"
keywords = ["fixture"]
{build_section}[runtime]
image = "{PINNED_IMAGE}"
command = ["./server"]
'''.encode()


@pytest.mark.parametrize(
    ("identity", "canonical", "ref"),
    [
        ("owner/repo", "github:owner/repo", None),
        ("owner/repo@main", "github:owner/repo", "main"),
        ("github:owner/repo@v2.0.0", "github:owner/repo", "v2.0.0"),
        (
            "https://github.com/owner/repo.git@develop",
            "github:owner/repo",
            "develop",
        ),
    ],
)
def test_github_identity_normalization(
    identity: str, canonical: str, ref: str | None
) -> None:
    source = normalize_git_source(identity)
    assert source.canonical == canonical
    assert source.requested_ref == ref


def test_github_acquisition_fetches_only_declared_assets(tmp_path: Path) -> None:
    manifest_bytes = _manifest()
    template_bytes = b"motd=fixture\n"
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": REVISION})
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(200, content=manifest_bytes)
        if request.url.path.endswith("/contents/template"):
            return httpx.Response(
                200,
                json=[
                    {
                        "type": "file",
                        "path": "template/server.properties.tmpl",
                        "size": len(template_bytes),
                        "download_url": "https://objects.example/template",
                    }
                ],
            )
        if request.url.host == "objects.example":
            return httpx.Response(200, content=template_bytes)
        raise AssertionError(f"unexpected request: {request.url}")

    snapshot = acquire_github_recipe(
        normalize_git_source("owner/repo"),
        tmp_path / "recipe",
        ContentCache(tmp_path / "cache"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        allow_private_network=True,
    )

    assert snapshot.origin.kind == "github"
    assert snapshot.origin.revision == REVISION
    assert snapshot.origin.ref == "main"
    assert snapshot.origin.tracking is True
    assert (snapshot.root / "template/server.properties.tmpl").read_bytes() == (
        template_bytes
    )
    assert not any(
        "tarball" in path or "/src" in path or "/tests" in path for path in requested
    )


def test_github_acquisition_accepts_encoded_declared_assets(tmp_path: Path) -> None:
    manifest_bytes = _manifest()
    template_bytes = b"motd=compressed fixture\n"
    encoded_template = gzip.compress(template_bytes)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": REVISION})
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(200, content=manifest_bytes)
        if request.url.path.endswith("/contents/template"):
            return httpx.Response(
                200,
                json=[
                    {
                        "type": "file",
                        "path": "template/server.properties.tmpl",
                        "size": len(template_bytes),
                        "download_url": "https://objects.example/template",
                    }
                ],
            )
        if request.url.host == "objects.example":
            return httpx.Response(
                200,
                content=encoded_template,
                headers={
                    "content-encoding": "gzip",
                    "content-length": str(len(encoded_template)),
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    snapshot = acquire_github_recipe(
        normalize_git_source("owner/repo"),
        tmp_path / "recipe",
        ContentCache(tmp_path / "cache"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        allow_private_network=True,
    )

    assert len(encoded_template) != len(template_bytes)
    assert (snapshot.root / "template/server.properties.tmpl").read_bytes() == (
        template_bytes
    )


def test_github_tag_is_pinned_instead_of_tracked(tmp_path: Path) -> None:
    manifest_bytes = _manifest()
    template_bytes = b"motd=tag\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if "/branches/v2.0.0" in request.url.path:
            return httpx.Response(404)
        if request.url.path.endswith("/commits/v2.0.0"):
            return httpx.Response(200, json={"sha": REVISION})
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(200, content=manifest_bytes)
        if request.url.path.endswith("/contents/template"):
            return httpx.Response(
                200,
                json=[
                    {
                        "type": "file",
                        "path": "template/server.properties.tmpl",
                        "size": len(template_bytes),
                        "download_url": "https://objects.example/tag-template",
                    }
                ],
            )
        if request.url.host == "objects.example":
            return httpx.Response(200, content=template_bytes)
        raise AssertionError(f"unexpected request: {request.url}")

    snapshot = acquire_github_recipe(
        normalize_git_source("github:owner/repo@v2.0.0"),
        tmp_path / "recipe",
        ContentCache(tmp_path / "cache"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        allow_private_network=True,
    )

    assert snapshot.origin.ref == "v2.0.0"
    assert snapshot.origin.tracking is False


def test_github_build_uses_bounded_temporary_source_context(tmp_path: Path) -> None:
    manifest_bytes = _manifest(build=True, template=False)
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        for name, content in {
            "repo/luminesk.toml": manifest_bytes,
            "repo/.luminesk/Dockerfile": b"FROM scratch\nCOPY src /out\n",
            "repo/src/main.c": b"int main(void) { return 0; }\n",
            "repo/tests/test_main.c": b"tests\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    archive_bytes = archive_buffer.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": REVISION})
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(200, content=manifest_bytes)
        if request.url.path.endswith(f"/tarball/{REVISION}"):
            return httpx.Response(200, content=archive_bytes)
        raise AssertionError(f"unexpected request: {request.url}")

    snapshot = acquire_github_recipe(
        normalize_git_source("owner/repo"),
        tmp_path / "recipe",
        ContentCache(tmp_path / "cache"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        allow_private_network=True,
    )

    assert (snapshot.root / "src/main.c").is_file()
    assert {entry.path for entry in snapshot.entries} == {
        ".luminesk/Dockerfile",
        "luminesk.toml",
    }


@pytest.mark.parametrize("identity", ["fixture", "db:fixture"])
def test_database_identity_installs_from_active_snapshot(
    identity: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from luminesk_cli.cli.commands import install as install_command

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    recipe_root = tmp_path / "database-recipe"
    (recipe_root / "template").mkdir(parents=True)
    manifest_bytes = _manifest()
    (recipe_root / "luminesk.toml").write_bytes(manifest_bytes)
    (recipe_root / "template/server.properties.tmpl").write_bytes(b"motd=fixture\n")
    manifest = parse_manifest(manifest_bytes)
    tree = read_template_tree(recipe_root, manifest)
    assert tree is not None
    recipe_snapshot = create_recipe_snapshot(
        recipe_root,
        manifest,
        kind="database",
        source="github:farin-works/luminesk-database",
        revision=REVISION,
        tracking=True,
        entry="fixture",
        path="database/fixture",
    )
    index_content = (
        json.dumps(
            {
                "indexVersion": 1,
                "revision": REVISION,
                "entries": [
                    {
                        "name": "fixture",
                        "displayName": "Fixture",
                        "recipeVersion": "2.0.0",
                        "kind": "core",
                        "game": "minecraft",
                        "edition": "java",
                        "summary": "Fixture server",
                        "keywords": ["fixture"],
                        "path": "database/fixture",
                        "manifestDigest": sha256_digest(manifest_bytes),
                        "templateDigest": tree.digest,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    store_root = tmp_path / "cache-home" / "luminesk_cli" / "v2" / "catalog"
    CatalogStore(store_root).commit(parse_catalog_index(index_content), index_content)

    class FakeCatalogClient:
        def __init__(self, store):
            assert store.root == store_root

        def acquire_entry(self, catalog, entry, destination):
            del destination
            assert catalog.revision == REVISION
            assert entry.name == "fixture"
            return recipe_snapshot

    monkeypatch.setattr(install_command, "CatalogClient", FakeCatalogClient)
    target = tmp_path / f"instance-{identity.replace(':', '-')}"

    assert main(["i", identity, "--dir", str(target), "--yes", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert (target / "server.properties").read_bytes() == b"motd=fixture\n"
    assert (target / ".luminesk_cli/recipe/template/server.properties.tmpl").is_file()
    assert not (target / "src").exists()
    assert not (target / "tests").exists()

    if identity == "fixture":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "frozen-config-home"))
        frozen_target = tmp_path / "frozen-instance"
        assert (
            main(
                [
                    "i",
                    "fixture",
                    "--dir",
                    str(frozen_target),
                    "--frozen",
                    "--yes",
                    "--json",
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["ok"] is True
        assert (frozen_target / "server.properties").read_bytes() == b"motd=fixture\n"


def test_github_identity_never_falls_back_to_database(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from luminesk_cli.cli.commands import install as install_command

    calls: list[str] = []

    def fail_github(source, destination, cache):
        del destination, cache
        calls.append(source.canonical)
        raise ResolutionError("direct GitHub marker")

    monkeypatch.setattr(install_command, "acquire_github_recipe", fail_github)
    monkeypatch.setattr(
        install_command,
        "catalog_store",
        lambda: (_ for _ in ()).throw(AssertionError("database fallback")),
    )

    assert main(["i", "owner/repo", "--dir", str(tmp_path / "instance")]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "direct GitHub marker" in captured.err
    assert calls == ["github:owner/repo"]


def test_external_project_install_does_not_copy_unrelated_source_tree(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src/main.c").write_text("int main(void) { return 0; }\n")
    (project / "tests/test_main.c").write_text("tests\n")
    (project / "README.md").write_text("developer documentation\n")
    (project / "server.bin.in").write_bytes(b"server")
    (project / "luminesk.toml").write_text(
        f'''\
manifest_version = 1
[package]
name = "project-fixture"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "java"
[[sources]]
id = "server"
type = "local-file"
target = "server.bin"
[sources.options]
path = "server.bin.in"
[runtime]
image = "{PINNED_IMAGE}"
command = ["./server.bin"]
''',
        encoding="utf-8",
    )
    target = tmp_path / "instance"

    assert (
        main(
            [
                "i",
                str(project),
                "--dir",
                str(target),
                "--yes",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert (target / "server.bin").read_bytes() == b"server"
    assert not (target / "src").exists()
    assert not (target / "tests").exists()
    assert not (target / "README.md").exists()
