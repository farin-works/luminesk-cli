from __future__ import annotations

from pathlib import Path

import pytest

from luminesk_cli.application.install import TransactionalInstaller
from luminesk_cli.application.locking import LockService
from luminesk_cli.cli.commands.update import _managed_drift
from luminesk_cli.cli.commands.validate import _validate_instance
from luminesk_cli.domain.errors import ConflictError, TransactionError, ValidationError
from luminesk_cli.domain.instance import OwnershipEntry, OwnershipLedger
from luminesk_cli.domain.lockfile import Lockfile
from luminesk_cli.domain.manifest import Check, Manifest, parse_manifest
from luminesk_cli.domain.package import ServerPackage
from luminesk_cli.infrastructure.build import DeclarativeBuilder
from luminesk_cli.infrastructure.cache import ContentCache
from luminesk_cli.infrastructure.oci import OciImageResolver
from luminesk_cli.infrastructure.recipe_snapshot import create_recipe_snapshot
from luminesk_cli.infrastructure.state import (
    load_ownership,
    load_state,
    write_ownership,
)


def make_package(
    tmp_path: Path,
    version: str,
    content: bytes,
) -> tuple[Manifest, Lockfile, ServerPackage]:
    recipe = tmp_path / f"recipe-{version}"
    recipe.mkdir()
    (recipe / "fixture.jar").write_bytes(content)
    manifest_bytes = f'''\
manifest_version = 1
[package]
name = "fixture-server"
version = "{version}"
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
source = "worlds"
target = "worlds"
mode = "data"
[runtime]
image = "example/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["java", "-jar", "server.jar"]
[update]
backup = ["worlds"]
retain_backups = 2
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
        tmp_path / f"fixture-{version}.lumineskpkg",
    )
    return manifest, lockfile, package


def test_install_is_transactional_and_idempotent(tmp_path: Path) -> None:
    manifest, lockfile, package = make_package(tmp_path, "2.0.0", b"initial")
    target = tmp_path / "instance"
    installer = TransactionalInstaller()
    snapshot = create_recipe_snapshot(tmp_path / "recipe-2.0.0", manifest)

    plan, state = installer.install(
        manifest,
        lockfile,
        package,
        target,
        recipe_snapshot=snapshot,
    )

    assert state is not None
    assert (target / "server.jar").read_bytes() == b"initial"
    assert (target / ".luminesk_cli/state.json").is_file()
    assert (target / ".luminesk_cli/ownership.json").is_file()
    assert (target / ".luminesk_cli/recipe/luminesk.toml").read_bytes() == (
        target / "luminesk.toml"
    ).read_bytes()
    assert (target / ".luminesk_cli/recipe/fixture.jar").read_bytes() == b"initial"
    assert not (target / ".luminesk_cli/transaction.json").exists()
    assert any(change.action == "create" for change in plan.changes)

    second_plan, second_state = installer.install(
        manifest,
        lockfile,
        package,
        target,
        recipe_snapshot=snapshot,
    )

    assert second_state is not None
    assert second_state.instance_id == state.instance_id
    assert all(change.action == "preserve" for change in second_plan.changes)


def test_install_refuses_to_overwrite_user_drift(tmp_path: Path) -> None:
    first_manifest, first_lock, first_package = make_package(
        tmp_path, "2.0.0", b"initial"
    )
    target = tmp_path / "instance"
    installer = TransactionalInstaller()
    installer.install(first_manifest, first_lock, first_package, target)
    (target / "server.jar").write_bytes(b"user modified")
    second_manifest, second_lock, second_package = make_package(
        tmp_path, "2.1.0", b"replacement"
    )

    with pytest.raises(ConflictError, match="conflicts"):
        installer.install(
            second_manifest,
            second_lock,
            second_package,
            target,
        )

    assert (target / "server.jar").read_bytes() == b"user modified"
    state = load_state(target)
    assert state is not None
    assert state.installed_package_digest == first_package.digest


def test_instance_validation_ignores_user_owned_drift(tmp_path: Path) -> None:
    manifest, lockfile, package = make_package(tmp_path, "2.0.0", b"initial")
    target = tmp_path / "instance"
    TransactionalInstaller().install(manifest, lockfile, package, target)
    preserved = target / "server.properties"
    preserved.write_text("motd=initial\n", encoding="utf-8")
    ledger = load_ownership(target)
    write_ownership(
        target,
        OwnershipLedger(
            files={
                **ledger.files,
                "server.properties": OwnershipEntry(
                    mode="preserve",
                    digest="sha256:" + "a" * 64,
                ),
            }
        ),
    )

    preserved.write_text("motd=changed by server\n", encoding="utf-8")

    _validate_instance(target, manifest.digest)
    assert _managed_drift(target) == []


def test_instance_validation_still_rejects_managed_drift(tmp_path: Path) -> None:
    manifest, lockfile, package = make_package(tmp_path, "2.0.0", b"initial")
    target = tmp_path / "instance"
    TransactionalInstaller().install(manifest, lockfile, package, target)
    (target / "server.jar").write_bytes(b"changed outside Luminesk")

    with pytest.raises(ValidationError, match="managed-file drift"):
        _validate_instance(target, manifest.digest)

    assert _managed_drift(target) == [{"path": "server.jar", "status": "modified"}]


def test_failed_update_restores_files_and_metadata(tmp_path: Path) -> None:
    first_manifest, first_lock, first_package = make_package(
        tmp_path, "2.0.0", b"initial"
    )
    target = tmp_path / "instance"
    TransactionalInstaller().install(first_manifest, first_lock, first_package, target)
    old_state = load_state(target)
    old_ownership = load_ownership(target)
    second_manifest, second_lock, second_package = make_package(
        tmp_path, "2.1.0", b"replacement"
    )

    def fail_after_first_write(path: str) -> None:
        raise RuntimeError(f"injected failure after {path}")

    installer = TransactionalInstaller(apply_hook=fail_after_first_write)

    with pytest.raises(TransactionError, match="rolled back"):
        installer.install(
            second_manifest,
            second_lock,
            second_package,
            target,
        )

    assert (target / "server.jar").read_bytes() == b"initial"
    assert load_state(target) == old_state
    assert load_ownership(target) == old_ownership
    assert not (target / ".luminesk_cli/transaction.json").exists()


def test_failed_update_restores_canonical_recipe_snapshot(tmp_path: Path) -> None:
    from dataclasses import replace

    first_manifest, first_lock, first_package = make_package(
        tmp_path, "2.0.0", b"initial"
    )
    first_snapshot = create_recipe_snapshot(
        tmp_path / "recipe-2.0.0",
        first_manifest,
    )
    target = tmp_path / "instance"
    TransactionalInstaller().install(
        first_manifest,
        first_lock,
        first_package,
        target,
        recipe_snapshot=first_snapshot,
    )
    old_root_manifest = (target / "luminesk.toml").read_bytes()
    old_snapshot_manifest = (target / ".luminesk_cli/recipe/luminesk.toml").read_bytes()
    second_manifest, second_lock, second_package = make_package(
        tmp_path, "2.1.0", b"replacement"
    )
    second_snapshot = create_recipe_snapshot(
        tmp_path / "recipe-2.1.0",
        second_manifest,
    )
    failing_manifest = replace(
        second_manifest,
        checks=(
            Check(
                id="missing-after-snapshot",
                phase="post-install",
                kind="file",
                path="missing.txt",
            ),
        ),
    )

    with pytest.raises(TransactionError, match="rolled back"):
        TransactionalInstaller().install(
            failing_manifest,
            second_lock,
            second_package,
            target,
            recipe_snapshot=second_snapshot,
        )

    assert (target / "luminesk.toml").read_bytes() == old_root_manifest
    assert (
        target / ".luminesk_cli/recipe/luminesk.toml"
    ).read_bytes() == old_snapshot_manifest
    assert (target / "server.jar").read_bytes() == b"initial"


def test_dry_run_does_not_create_target(tmp_path: Path) -> None:
    manifest, lockfile, package = make_package(tmp_path, "2.0.0", b"server")
    target = tmp_path / "missing-instance"

    plan, state = TransactionalInstaller().install(
        manifest,
        lockfile,
        package,
        target,
        dry_run=True,
    )

    assert plan.operation == "install"
    assert state is None
    assert not target.exists()


def test_failed_post_install_check_rolls_back_new_instance(tmp_path: Path) -> None:
    from dataclasses import replace

    manifest, lockfile, package = make_package(tmp_path, "2.0.0", b"server")
    manifest = replace(
        manifest,
        checks=(
            Check(
                id="missing",
                phase="post-install",
                kind="file",
                path="required.txt",
            ),
        ),
    )
    target = tmp_path / "failed-instance"

    with pytest.raises(TransactionError, match="rolled back"):
        TransactionalInstaller().install(manifest, lockfile, package, target)

    assert not (target / "server.jar").exists()
    assert load_state(target) is None


def test_installer_rejects_package_bound_to_another_lock(tmp_path: Path) -> None:
    from dataclasses import replace

    manifest, lockfile, package = make_package(tmp_path, "2.0.0", b"server")
    package = replace(
        package,
        metadata=replace(
            package.metadata,
            lock_digest=f"sha256:{'f' * 64}",
        ),
    )

    with pytest.raises(ValidationError, match="lockfile"):
        TransactionalInstaller().install(
            manifest,
            lockfile,
            package,
            tmp_path / "instance",
        )
