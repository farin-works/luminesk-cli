from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from luminesk_cli.cli.entry import main
from luminesk_cli.domain.catalog import (
    CatalogEntry,
    CatalogSnapshot,
    parse_catalog_index,
    search_catalog,
)
from luminesk_cli.domain.errors import SecurityError, ValidationError
from luminesk_cli.domain.manifest import parse_manifest
from luminesk_cli.domain.primitives import sha256_digest
from luminesk_cli.infrastructure.catalog import CatalogClient, CatalogStore
from luminesk_cli.infrastructure.state import canonical_json_bytes
from luminesk_cli.infrastructure.template import read_template_tree

REVISION = "a" * 40
DISTRIBUTION_REVISION = "f" * 40


def _entry(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "lumi",
        "displayName": "Lumi",
        "recipeVersion": "1.0.1",
        "kind": "core",
        "game": "minecraft",
        "edition": "bedrock",
        "summary": "Lumi Minecraft server",
        "keywords": ["lumi", "bedrock"],
        "path": "database/lumi",
        "manifestDigest": f"sha256:{'b' * 64}",
        "license": "LGPL-3.0-only",
        "authors": ["Luminesk Test"],
        "platforms": ["linux/amd64", "linux/arm64"],
        "repository": "https://example.com/lumi",
        "sourceTypes": ["maven"],
        "runtimeImage": "example/lumi@sha256:" + "c" * 64,
    }
    value.update(overrides)
    return value


def _index(*entries: dict[str, object], revision: str = REVISION) -> bytes:
    return (
        json.dumps(
            {
                "indexVersion": 1,
                "revision": revision,
                "entries": list(entries or (_entry(),)),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def test_index_is_strict_and_rejects_duplicate_or_escaping_entries() -> None:
    snapshot = parse_catalog_index(_index(_entry()))
    assert snapshot.revision == REVISION
    assert snapshot.entries[0].edition == "bedrock"
    assert snapshot.entries[0].license == "LGPL-3.0-only"
    assert snapshot.entries[0].source_types == ("maven",)

    with pytest.raises(ValidationError, match="duplicate"):
        parse_catalog_index(_index(_entry(), _entry()))
    with pytest.raises(ValidationError, match="path"):
        parse_catalog_index(_index(_entry(path="../lumi")))
    with pytest.raises(ValidationError, match="unknown key"):
        parse_catalog_index(_index(_entry(unexpected="value")))


def test_search_ranking_and_filters_are_deterministic() -> None:
    content = _index(
        _entry(),
        _entry(
            name="paper",
            displayName="Paper",
            edition="java",
            summary="High performance Java server",
            keywords=["paper", "java"],
            path="database/paper",
        ),
    )
    snapshot = parse_catalog_index(content)

    assert [entry.name for entry in search_catalog(snapshot, "paper")] == ["paper"]
    assert [entry.name for entry in search_catalog(snapshot, "java performance")] == [
        "paper"
    ]
    assert [entry.name for entry in search_catalog(snapshot, edition="bedrock")] == [
        "lumi"
    ]


def test_store_keeps_verified_active_snapshot_offline(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "catalog")
    content = _index(_entry())
    snapshot = parse_catalog_index(content)

    store.commit(snapshot, content)

    assert store.load_active() == snapshot
    assert store.verify() == snapshot
    assert store.use(REVISION) == snapshot


def test_store_rejects_immutable_revision_collision(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "catalog")
    content = _index(_entry())
    snapshot = parse_catalog_index(content)
    store.commit(snapshot, content)

    conflicting_content = _index(_entry(displayName="Changed"))
    conflicting_snapshot = parse_catalog_index(conflicting_content)
    with pytest.raises(SecurityError, match="collision"):
        store.commit(conflicting_snapshot, conflicting_content)

    assert store.load_active() == snapshot


def test_store_reuses_pre_rename_snapshot_without_rewriting_it(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "catalog")
    content = _index(_entry())
    snapshot = parse_catalog_index(content)
    store.commit(snapshot, content)
    metadata_path = store.root / "snapshots" / REVISION / "snapshot.json"
    old_metadata = canonical_json_bytes(
        {
            "repository": "github:task-v1/luminesk-database",
            "revision": REVISION,
            "indexDigest": snapshot.index_digest,
        }
    )
    metadata_path.write_bytes(old_metadata)
    store.active_path.write_bytes(old_metadata)

    assert store.verify().repository == "github:farin-works/luminesk-database"
    assert store.use(REVISION) == snapshot
    store.commit(snapshot, content)
    assert metadata_path.read_bytes() == old_metadata
    assert store.verify() == snapshot
    assert json.loads(store.active_path.read_bytes())["repository"] == (
        "github:farin-works/luminesk-database"
    )


@pytest.mark.parametrize(
    "target", ["active.json", f"snapshots/{REVISION}/snapshot.json"]
)
def test_store_rejects_unofficial_repository_after_rename(
    tmp_path: Path, target: str
) -> None:
    store = CatalogStore(tmp_path / "catalog")
    content = _index(_entry())
    store.commit(parse_catalog_index(content), content)
    path = store.root / target
    metadata = json.loads(path.read_bytes())
    metadata["repository"] = "github:untrusted/luminesk-database"
    path.write_bytes(canonical_json_bytes(metadata))

    with pytest.raises((ValidationError, SecurityError), match="not official"):
        store.verify()


def test_store_use_verifies_cached_snapshot_metadata(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "catalog")
    content = _index(_entry())
    snapshot = parse_catalog_index(content)
    store.commit(snapshot, content)
    index = tmp_path / "catalog" / "snapshots" / REVISION / "index-v1.json"
    index.write_bytes(_index(_entry(displayName="Tampered")))

    with pytest.raises(SecurityError, match="digest"):
        store.use(REVISION)


def test_catalog_update_verifies_digest_and_is_atomic(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "catalog")
    old_content = _index(_entry(), revision="b" * 40)
    old_snapshot = parse_catalog_index(old_content)
    store.commit(old_snapshot, old_content)
    new_content = _index(_entry())
    digest = hashlib.sha256(new_content).hexdigest()
    requested_paths: list[str] = []

    def valid_handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith("/luminesk-database"):
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": DISTRIBUTION_REVISION})
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=digest)
        return httpx.Response(200, content=new_content)

    client = httpx.Client(transport=httpx.MockTransport(valid_handler))
    updated = CatalogClient(
        store,
        client=client,
        allow_private_network=True,
    ).update()
    assert updated.revision == REVISION
    assert store.load_active() == updated
    assert updated.repository == "github:farin-works/luminesk-database"
    assert all("/farin-works/luminesk-database" in path for path in requested_paths)
    assert any(
        f"/{DISTRIBUTION_REVISION}/dist/index-v1.json" in path
        for path in requested_paths
    )

    bad_revision = "c" * 40

    def corrupt_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/luminesk-database"):
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": bad_revision})
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text="d" * 64)
        return httpx.Response(200, content=b"corrupt")

    with pytest.raises(SecurityError, match="digest"):
        CatalogClient(
            store,
            client=httpx.Client(transport=httpx.MockTransport(corrupt_handler)),
            allow_private_network=True,
        ).update()
    assert store.load_active() == updated


def test_search_info_and_catalog_status_emit_stable_json(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = tmp_path / "cache" / "luminesk_cli" / "v2" / "catalog"
    content = _index(_entry())
    CatalogStore(root).commit(parse_catalog_index(content), content)
    manifest = parse_manifest(
        b"""\
manifest_version = 1
template = "template"
[package]
name = "lumi"
version = "1.0.1"
display_name = "Lumi"
kind = "core"
game = "minecraft"
edition = "bedrock"
summary = "Lumi Minecraft server"
keywords = ["lumi", "bedrock"]
[inputs.server_name]
type = "string"
default = "Lumi Server"
prompt = "Server name"
[inputs.token]
type = "string"
required = true
secret = true
prompt = "Provider token"
[runtime]
image = "example/server:2"
command = ["./server"]
"""
    )
    monkeypatch.setattr(
        CatalogClient,
        "fetch_entry_manifest",
        lambda self, snapshot, entry: manifest,
    )

    assert main(["search", "lumi", "--edition", "bedrock", "--json"]) == 0
    search = json.loads(capsys.readouterr().out)
    assert search["recipes"][0]["name"] == "lumi"
    assert search["total"] == 1
    assert search["returned"] == 1
    assert search["catalogActivatedAt"].endswith("Z")
    assert "namespace" not in search["recipes"][0]

    assert main(["info", "lumi", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["recipe"]["recipeVersion"] == "1.0.1"
    assert info["recipe"]["license"] == "LGPL-3.0-only"
    assert info["recipe"]["inputs"] == [
        {
            "default": "Lumi Server",
            "maximum": None,
            "minimum": None,
            "name": "server_name",
            "option": "--set",
            "pattern": None,
            "prompt": "Server name",
            "required": False,
            "secret": False,
            "type": "string",
        },
        {
            "default": None,
            "maximum": None,
            "minimum": None,
            "name": "token",
            "option": "--set-file",
            "pattern": None,
            "prompt": "Provider token",
            "required": True,
            "secret": True,
            "type": "string",
        },
    ]

    assert main(["catalog", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["available"] is True
    assert status["revision"] == REVISION


def test_search_limits_human_table_and_suggests_typos(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = tmp_path / "cache" / "luminesk_cli" / "v2" / "catalog"
    paper = _entry(
        name="paper",
        displayName="Paper",
        edition="java",
        summary="High performance Java server",
        keywords=["paper", "java"],
        path="database/paper",
    )
    content = _index(_entry(), paper)
    CatalogStore(root).commit(parse_catalog_index(content), content)

    assert main(["search", "--limit", "1"]) == 0
    output = capsys.readouterr().out
    assert "NAME" in output
    assert "VERSION" in output
    assert "Showing 1 of 2" in output

    assert main(["search", "papre", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["recipes"] == []
    assert payload["suggestions"] == ["paper"]

    assert main(["info", "papre", "--json"]) == 3
    error = json.loads(capsys.readouterr().out)
    assert "Did you mean: paper?" in error["error"]["message"]


def test_catalog_entry_fetches_only_declared_template(tmp_path: Path) -> None:
    manifest_bytes = b"""\
manifest_version = 1
template = "template"
[package]
name = "lumi"
version = "1.0.1"
display_name = "Lumi"
kind = "core"
game = "minecraft"
edition = "bedrock"
summary = "Lumi Minecraft server"
keywords = ["lumi", "bedrock"]
[runtime]
image = "example/server:2"
command = ["./server"]
"""
    template_content = b"server-name=${input.server_name}\n"
    digest_root = tmp_path / "digest-recipe"
    (digest_root / "template").mkdir(parents=True)
    (digest_root / "luminesk.toml").write_bytes(manifest_bytes)
    (digest_root / "template/settings.yml.tmpl").write_bytes(template_content)
    manifest = parse_manifest(manifest_bytes)
    tree = read_template_tree(digest_root, manifest)
    assert tree is not None
    entry = CatalogEntry(
        name="lumi",
        display_name="Lumi",
        recipe_version="1.0.1",
        kind="core",
        game="minecraft",
        edition="bedrock",
        summary="Lumi Minecraft server",
        keywords=("lumi", "bedrock"),
        path="database/lumi",
        manifest_digest=sha256_digest(manifest_bytes),
        template_digest=tree.digest,
    )
    snapshot = CatalogSnapshot(
        revision=REVISION,
        entries=(entry,),
        index_digest=f"sha256:{'e' * 64}",
    )
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(200, content=manifest_bytes)
        if "/contents/database/lumi/template" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {
                        "type": "file",
                        "path": "database/lumi/template/settings.yml.tmpl",
                        "size": len(template_content),
                        "download_url": "https://objects.example/settings.yml.tmpl",
                    }
                ],
            )
        return httpx.Response(200, content=template_content)

    recipe = CatalogClient(
        CatalogStore(tmp_path / "catalog"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        allow_private_network=True,
    ).acquire_entry(snapshot, entry, tmp_path / "recipe")

    assert recipe.origin.kind == "database"
    assert recipe.origin.entry == "lumi"
    assert (recipe.root / "template/settings.yml.tmpl").read_bytes() == template_content
    assert not any("src" in path or "tests" in path for path in requested_paths)


def test_catalog_manifest_inspection_fetches_no_recipe_assets(tmp_path: Path) -> None:
    manifest_bytes = b"""\
manifest_version = 1
template = "template"
[package]
name = "lumi"
version = "1.0.1"
display_name = "Lumi"
kind = "core"
game = "minecraft"
edition = "bedrock"
summary = "Lumi Minecraft server"
keywords = ["lumi", "bedrock"]
[inputs.port]
type = "integer"
default = 19132
min = 1
max = 65535
prompt = "Game port"
[runtime]
image = "example/server:2"
command = ["./server"]
"""
    entry = CatalogEntry(
        name="lumi",
        display_name="Lumi",
        recipe_version="1.0.1",
        kind="core",
        game="minecraft",
        edition="bedrock",
        summary="Lumi Minecraft server",
        keywords=("lumi", "bedrock"),
        path="database/lumi",
        manifest_digest=sha256_digest(manifest_bytes),
    )
    snapshot = CatalogSnapshot(
        revision=REVISION,
        entries=(entry,),
        index_digest=f"sha256:{'e' * 64}",
    )
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, content=manifest_bytes)

    client = CatalogClient(
        CatalogStore(tmp_path / "catalog"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        allow_private_network=True,
    )

    manifest = client.fetch_entry_manifest(snapshot, entry)

    assert manifest.inputs[0].name == "port"
    assert requests == [
        f"/farin-works/luminesk-database/{REVISION}/database/lumi/luminesk.toml"
    ]
