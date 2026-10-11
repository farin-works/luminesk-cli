from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from luminesk_cli.application.install import TransactionalInstaller
from luminesk_cli.application.locking import LockService
from luminesk_cli.application.runtime import DockerRuntime
from luminesk_cli.application.update import UpdateService
from luminesk_cli.cli.commands import update as update_command
from luminesk_cli.cli.commands.update import (
    RECIPE_VERSION_WARNING,
    _candidate_recipe,
    _database_entry_matches_lock,
    _lock_changes,
    _recipe_warnings,
    _security_changes,
)
from luminesk_cli.domain.catalog import CatalogEntry, CatalogSnapshot
from luminesk_cli.domain.errors import (
    ConflictError,
    RuntimeOperationError,
    TransactionError,
)
from luminesk_cli.domain.instance import InstanceState, RuntimeState
from luminesk_cli.domain.lockfile import (
    Lockfile,
    RecipeLock,
    ResolvedSource,
    RuntimeLock,
)
from luminesk_cli.domain.manifest import Manifest, parse_manifest
from luminesk_cli.domain.package import ServerPackage
from luminesk_cli.domain.recipe import RecipeSnapshot
from luminesk_cli.infrastructure.build import DeclarativeBuilder
from luminesk_cli.infrastructure.cache import ContentCache
from luminesk_cli.infrastructure.oci import OciImageResolver
from luminesk_cli.infrastructure.recipe_snapshot import create_recipe_snapshot
from luminesk_cli.infrastructure.state import load_state, write_state


def make_release(
    tmp_path: Path,
    version: str,
    artifact: bytes,
) -> tuple[Path, Manifest, Lockfile, ServerPackage]:
    recipe = tmp_path / f"recipe-{version}"
    recipe.mkdir()
    (recipe / "artifact.bin").write_bytes(artifact)
    manifest_bytes = f'''\
manifest_version = 1
[package]
name = "update-fixture"
version = "{version}"
kind = "core"
game = "minecraft"
edition = "bedrock"
[[sources]]
id = "core"
type = "local-file"
target = "server.bin"
[sources.options]
path = "artifact.bin"
[runtime]
image = "example/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["./server.bin"]
'''.encode()
    (recipe / "luminesk.toml").write_bytes(manifest_bytes)
    manifest = parse_manifest(manifest_bytes)
    cache = ContentCache(tmp_path / "cache")
    snapshot = create_recipe_snapshot(recipe, manifest)
    lockfile = LockService(cache, image_resolver=OciImageResolver()).create(
        manifest,
        recipe,
        recipe_origin=snapshot.origin,
        target="linux/amd64",
    )
    package = DeclarativeBuilder(cache).build(
        manifest,
        lockfile,
        recipe,
        tmp_path / f"update-{version}.lumineskpkg",
    )
    return recipe, manifest, lockfile, package


class FailingThenRecoveringRuntime(DockerRuntime):
    def __init__(self, failed_package_digest: str) -> None:
        super().__init__()
        self.failed_package_digest = failed_package_digest
        self.failure_seen = False

    def status(self, root: Path) -> InstanceState:
        state = load_state(root)
        assert state is not None
        return state

    def stop(self, root: Path, *, remove: bool = False) -> InstanceState:
        del remove
        state = load_state(root)
        assert state is not None
        stopped = replace(
            state,
            runtime=RuntimeState(driver="docker", status="stopped"),
        )
        write_state(root, stopped)
        return stopped

    def start(
        self,
        root: Path,
        *,
        input_overrides: Mapping[str, str | int | bool] | None = None,
        wait_for_readiness: bool = True,
    ) -> InstanceState:
        del input_overrides, wait_for_readiness
        state = load_state(root)
        assert state is not None

        if state.installed_package_digest == self.failed_package_digest:
            self.failure_seen = True
            raise RuntimeOperationError("injected readiness failure")

        running = replace(
            state,
            runtime=RuntimeState(
                driver="docker",
                container_id="restored-container",
                status="running",
            ),
        )
        write_state(root, running)
        return running


def install_release(tmp_path: Path) -> Path:
    recipe, manifest, lockfile, package = make_release(tmp_path, "2.0.0", b"healthy")
    target = tmp_path / "instance"
    TransactionalInstaller().install(
        manifest,
        lockfile,
        package,
        target,
        recipe_snapshot=create_recipe_snapshot(recipe, manifest),
    )
    return target


def recovery_namespace(
    target: Path, *, force_clean: bool, yes: bool
) -> SimpleNamespace:
    return SimpleNamespace(
        dir=str(target),
        force_clean=force_clean,
        yes=yes,
        json=False,
        non_interactive=True,
    )


def test_recover_refuses_retained_backup_without_active_journal(
    tmp_path: Path,
) -> None:
    target = install_release(tmp_path)
    protected_paths = (
        target / "server.bin",
        target / "luminesk.lock",
        target / ".luminesk_cli/state.json",
        target / ".luminesk_cli/ownership.json",
    )
    original_contents = {path: path.read_bytes() for path in protected_paths}

    with pytest.raises(TransactionError, match="no active transaction journal"):
        update_command.recover(recovery_namespace(target, force_clean=False, yes=False))

    assert {path: path.read_bytes() for path in protected_paths} == original_contents


def test_recover_refuses_stale_journal_for_committed_instance(tmp_path: Path) -> None:
    target = install_release(tmp_path)
    backup = next((target / ".luminesk_cli/backups").iterdir())
    journal = target / ".luminesk_cli/transaction.json"
    journal.write_text(f'{{"id":"{backup.name}"}}', encoding="utf-8")

    with pytest.raises(TransactionError, match="no matching pending transaction"):
        update_command.recover(recovery_namespace(target, force_clean=False, yes=False))

    assert (target / "server.bin").read_bytes() == b"healthy"
    assert load_state(target) is not None


def test_recover_restores_backup_for_active_transaction(tmp_path: Path) -> None:
    target = install_release(tmp_path)
    backup = next((target / ".luminesk_cli/backups").iterdir())
    state = load_state(target)
    assert state is not None
    write_state(target, replace(state, pending_transaction=backup.name))
    journal = target / ".luminesk_cli/transaction.json"
    journal.write_text(f'{{"id":"{backup.name}"}}', encoding="utf-8")

    assert (
        update_command.recover(recovery_namespace(target, force_clean=False, yes=False))
        == 0
    )
    assert not journal.exists()
    assert not (target / "server.bin").exists()
    assert load_state(target) is None


def test_force_clean_recovery_requires_confirmation(tmp_path: Path) -> None:
    target = install_release(tmp_path)
    namespace = recovery_namespace(target, force_clean=True, yes=False)

    with pytest.raises(ConflictError, match="requires --yes"):
        update_command.recover(namespace)

    assert (target / "server.bin").read_bytes() == b"healthy"
    namespace.yes = True

    assert update_command.recover(namespace) == 0
    assert not (target / "server.bin").exists()
    assert load_state(target) is None


def test_failed_readiness_restores_previous_running_instance(tmp_path: Path) -> None:
    old_recipe, old_manifest, old_lock, old_package = make_release(
        tmp_path, "2.0.0", b"working"
    )
    target = tmp_path / "instance"
    target.mkdir()
    (target / "luminesk.toml").write_bytes((old_recipe / "luminesk.toml").read_bytes())
    _, old_state = TransactionalInstaller().install(
        old_manifest,
        old_lock,
        old_package,
        target,
        recipe_snapshot=create_recipe_snapshot(old_recipe, old_manifest),
    )
    assert old_state is not None
    old_state = replace(
        old_state,
        runtime=RuntimeState(
            driver="docker",
            container_id="old-container",
            status="running",
        ),
    )
    write_state(target, old_state)
    _, new_manifest, new_lock, new_package = make_release(tmp_path, "2.1.0", b"broken")
    runtime = FailingThenRecoveringRuntime(new_package.digest)
    service = UpdateService(runtime=runtime)
    new_snapshot = create_recipe_snapshot(
        tmp_path / "recipe-2.1.0",
        new_manifest,
    )

    with pytest.raises(TransactionError, match="previous instance was restored"):
        service.update(
            target,
            new_manifest,
            new_lock,
            new_package,
            inputs={},
            recipe_snapshot=new_snapshot,
        )

    restored = load_state(target)
    assert runtime.failure_seen
    assert restored is not None
    assert restored.installed_package_digest == old_package.digest
    assert restored.runtime.status == "running"
    assert (target / "server.bin").read_bytes() == b"working"
    assert (target / "luminesk.lock").read_bytes() == old_lock.to_bytes()
    assert (target / "luminesk.toml").read_bytes() == (
        old_recipe / "luminesk.toml"
    ).read_bytes()
    assert (target / ".luminesk_cli/recipe/artifact.bin").read_bytes() == b"working"


def test_security_sensitive_endpoint_changes_are_explicit() -> None:
    def manifest(version: str, repository: str, image: str) -> Manifest:
        return parse_manifest(
            f'''\
manifest_version = 1
[package]
name = "endpoint-fixture"
version = "{version}"
kind = "core"
game = "minecraft"
edition = "java"
[[sources]]
id = "core"
type = "maven"
target = "server.jar"
[sources.options]
repository = "{repository}"
group = "example"
artifact = "server"
version = "latest"
[runtime]
image = "{image}"
command = ["java", "-jar", "server.jar"]
'''.encode()
        )

    changes = _security_changes(
        manifest("1.0.0", "https://old.example/releases", "old/server:1"),
        manifest("1.0.1", "https://new.example/releases", "new/server:1"),
    )

    assert changes == [
        {
            "field": "sources.core.repository",
            "from": "https://old.example/releases",
            "to": "https://new.example/releases",
        },
        {
            "field": "runtime.imageRepository",
            "from": "old/server",
            "to": "new/server",
        },
    ]


def update_lock(
    *,
    revision: str,
    recipe_version: str,
    recipe_digest: str,
    artifact_version: str,
    artifact_digest: str,
    kind: Literal["database", "github", "local"] = "github",
) -> Lockfile:
    recipe = RecipeLock(
        kind=kind,
        source=(
            "github:example/server"
            if kind == "github"
            else "github:task-v1/luminesk-database"
        ),
        revision=revision,
        ref="main" if kind == "github" else None,
        tracking=True,
        entry="server" if kind == "database" else None,
        path="database/server" if kind == "database" else None,
        version=recipe_version,
        manifest_digest=recipe_digest,
    )
    return Lockfile(
        manifest_digest=recipe_digest,
        target="linux/amd64",
        sources={
            "core": ResolvedSource(
                type="http",
                version=artifact_version,
                source_revision=artifact_version,
                url="https://example.invalid/server.bin",
                size=1,
                digest=artifact_digest,
                target="server.bin",
            )
        },
        runtime=RuntimeLock(
            image="example/server@sha256:" + "f" * 64,
        ),
        recipe=recipe,
    )


def test_lock_changes_separate_recipe_artifact_and_combined_updates() -> None:
    old = update_lock(
        revision="a" * 40,
        recipe_version="1.0.0",
        recipe_digest="sha256:" + "a" * 64,
        artifact_version="1.0.0",
        artifact_digest="sha256:" + "b" * 64,
    )
    recipe_only = update_lock(
        revision="b" * 40,
        recipe_version="1.0.1",
        recipe_digest="sha256:" + "c" * 64,
        artifact_version="1.0.0",
        artifact_digest="sha256:" + "b" * 64,
    )
    artifact_only = update_lock(
        revision="a" * 40,
        recipe_version="1.0.0",
        recipe_digest="sha256:" + "a" * 64,
        artifact_version="1.1.0",
        artifact_digest="sha256:" + "d" * 64,
    )
    combined = update_lock(
        revision="b" * 40,
        recipe_version="1.0.1",
        recipe_digest="sha256:" + "c" * 64,
        artifact_version="1.1.0",
        artifact_digest="sha256:" + "d" * 64,
    )

    assert [item["component"] for item in _lock_changes(old, recipe_only)] == [
        "recipe version",
        "recipe revision",
    ]
    assert [item["component"] for item in _lock_changes(old, artifact_only)] == ["core"]
    assert [item["component"] for item in _lock_changes(old, combined)] == [
        "recipe version",
        "recipe revision",
        "core",
    ]


def test_direct_recipe_content_change_without_version_bump_warns() -> None:
    old = update_lock(
        revision="a" * 40,
        recipe_version="1.0.0",
        recipe_digest="sha256:" + "a" * 64,
        artifact_version="1.0.0",
        artifact_digest="sha256:" + "b" * 64,
    )
    changed = update_lock(
        revision="b" * 40,
        recipe_version="1.0.0",
        recipe_digest="sha256:" + "c" * 64,
        artifact_version="1.0.0",
        artifact_digest="sha256:" + "b" * 64,
    )
    database_changed = update_lock(
        revision="b" * 40,
        recipe_version="1.0.0",
        recipe_digest="sha256:" + "c" * 64,
        artifact_version="1.0.0",
        artifact_digest="sha256:" + "b" * 64,
        kind="database",
    )

    assert _recipe_warnings(old, changed) == [RECIPE_VERSION_WARNING]
    assert _recipe_warnings(old, database_changed) == []


def test_database_entries_update_independently_of_catalog_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_lock = update_lock(
        revision="a" * 40,
        recipe_version="1.0.0",
        recipe_digest="sha256:" + "a" * 64,
        artifact_version="1.0.0",
        artifact_digest="sha256:" + "b" * 64,
        kind="database",
    )
    recipe = installed_lock.recipe
    assert recipe is not None
    entry = CatalogEntry(
        name="server",
        display_name="Server",
        recipe_version="1.0.0",
        kind="core",
        game="minecraft",
        edition="java",
        summary="Server fixture",
        keywords=("server",),
        path="database/server",
        manifest_digest="sha256:" + "a" * 64,
    )
    catalog = CatalogSnapshot(
        revision="b" * 40,
        entries=(entry,),
        index_digest="sha256:" + "c" * 64,
    )

    class Store:
        def load_active(self) -> CatalogSnapshot:
            return catalog

    store = Store()
    monkeypatch.setattr(update_command, "catalog_store", lambda: store)

    def reject_acquisition(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("unchanged database entry was fetched")

    monkeypatch.setattr(update_command, "CatalogClient", reject_acquisition)
    installed = cast(RecipeSnapshot, object())

    assert (
        _candidate_recipe(installed_lock, installed, tmp_path / "candidate")
        is installed
    )
    assert _database_entry_matches_lock(recipe, entry)
    assert not _database_entry_matches_lock(
        recipe,
        replace(entry, recipe_version="1.0.1"),
    )
    assert not _database_entry_matches_lock(
        recipe,
        replace(entry, manifest_digest="sha256:" + "d" * 64),
    )
    assert not _database_entry_matches_lock(
        recipe,
        replace(entry, template_digest="sha256:" + "e" * 64),
    )
