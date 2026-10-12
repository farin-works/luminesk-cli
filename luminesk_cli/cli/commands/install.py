from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from luminesk_cli.application.install import TransactionalInstaller
from luminesk_cli.cli.commands.common import (
    build_package,
    cache,
    catalog_store,
    emit,
    index_path,
    recipe,
    recipe_cache,
    resolve_lock,
    validate_frozen_lock,
)
from luminesk_cli.cli.input_wizard import collect_install_inputs
from luminesk_cli.cli.output import print_human
from luminesk_cli.cli.progress import activity
from luminesk_cli.domain.errors import ConflictError, ValidationError
from luminesk_cli.domain.lockfile import Lockfile
from luminesk_cli.domain.preview import Preview
from luminesk_cli.domain.primitives import PACKAGE_NAME_RE
from luminesk_cli.domain.recipe import RecipeSnapshot
from luminesk_cli.infrastructure.catalog import CatalogClient
from luminesk_cli.infrastructure.platform import container_platform
from luminesk_cli.infrastructure.recipe import (
    acquire_github_recipe,
    ensure_empty_target,
    normalize_git_source,
)
from luminesk_cli.infrastructure.recipe_cache import database_locator, github_locator
from luminesk_cli.infrastructure.recipe_snapshot import create_recipe_snapshot
from luminesk_cli.infrastructure.state import InstanceIndex

LOGGER = logging.getLogger(__name__)


def run(namespace: Any) -> int:
    target = Path(namespace.dir or ".").expanduser().resolve()
    if namespace.source is None:
        LOGGER.debug("install source selected kind=local in_place=true")
        root, manifest = recipe(target)
        return _install_snapshot(
            namespace,
            create_recipe_snapshot(root, manifest),
            target,
            confirm=False,
        )

    raw_source = namespace.source.strip()
    source_path = Path(raw_source).expanduser()
    if source_path.exists():
        LOGGER.debug("install source selected kind=local in_place=false")
        if namespace.ref is not None:
            raise ValidationError("--ref is valid only for direct GitHub recipes")
        recipe_root = source_path.resolve()
        root, manifest = recipe(recipe_root)
        if recipe_root != target:
            ensure_empty_target(target)
        return _install_snapshot(
            namespace,
            create_recipe_snapshot(root, manifest),
            target,
            confirm=recipe_root != target,
        )
    if _looks_like_local_path(raw_source):
        raise ValidationError(f"local recipe path does not exist: {raw_source}")

    database_name = _database_name(raw_source)
    if database_name is not None:
        LOGGER.debug("install source selected kind=database")
        if namespace.ref is not None:
            raise ValidationError("--ref is valid only for direct GitHub recipes")
        ensure_empty_target(target)
        catalog = catalog_store().load_active()
        entry = next(
            (
                candidate
                for candidate in catalog.entries
                if candidate.name == database_name
            ),
            None,
        )
        if entry is None:
            raise ValidationError(f"catalog recipe not found: {database_name}")
        locator = database_locator(catalog.revision, entry.name)
        if namespace.frozen:
            LOGGER.debug("database recipe cache load started frozen=true")
            with activity(namespace, "Connecting to Docker Engine"):
                cached = recipe_cache().load_locator(locator, container_platform())
            return _install_snapshot(
                namespace,
                cached.snapshot,
                target,
                confirm=True,
                cached_lock=cached.lockfile,
            )
        with tempfile.TemporaryDirectory(
            prefix="luminesk-database-recipe-"
        ) as temporary:
            LOGGER.debug("database recipe acquisition started")
            with activity(namespace, "Downloading and verifying the recipe"):
                snapshot = CatalogClient(catalog_store()).acquire_entry(
                    catalog,
                    entry,
                    Path(temporary) / "recipe",
                )
            return _install_snapshot(
                namespace,
                snapshot,
                target,
                confirm=True,
                cache_locator=locator,
            )

    ensure_empty_target(target)
    LOGGER.debug("install source selected kind=github")
    source = normalize_git_source(raw_source, namespace.ref)
    locator = github_locator(source.canonical, source.requested_ref)
    if namespace.frozen:
        LOGGER.debug("github recipe cache load started frozen=true")
        with activity(namespace, "Connecting to Docker Engine"):
            cached = recipe_cache().load_locator(locator, container_platform())
        return _install_snapshot(
            namespace,
            cached.snapshot,
            target,
            confirm=True,
            cached_lock=cached.lockfile,
        )
    with tempfile.TemporaryDirectory(prefix="luminesk-github-recipe-") as temporary:
        LOGGER.debug("github recipe acquisition started")
        with activity(namespace, "Downloading and verifying the recipe"):
            snapshot = acquire_github_recipe(
                source,
                Path(temporary) / "recipe",
                cache(),
            )
        return _install_snapshot(
            namespace,
            snapshot,
            target,
            confirm=True,
            cache_locator=locator,
        )


def _database_name(source: str) -> str | None:
    explicit = source.startswith("db:")
    name = source.removeprefix("db:") if explicit else source
    if not explicit and (
        "/" in source or source.startswith("github:") or source.startswith("https://")
    ):
        return None
    if PACKAGE_NAME_RE.fullmatch(name) is None:
        raise ValidationError(
            "database recipe name must be a lowercase ASCII identifier"
        )
    return name


def _looks_like_local_path(source: str) -> bool:
    return source.startswith((".", "~", "/")) or "\\" in source


def _install_snapshot(
    namespace: Any,
    snapshot: RecipeSnapshot,
    target: Path,
    *,
    confirm: bool,
    cached_lock: Lockfile | None = None,
    cache_locator: str | None = None,
) -> int:
    root = snapshot.root
    manifest = snapshot.manifest
    origin = snapshot.origin
    values = collect_install_inputs(
        manifest,
        namespace.set,
        namespace.set_file,
        interactive=not namespace.non_interactive and not namespace.json,
    )
    LOGGER.debug(
        "install input parsing completed overrides=%d file_overrides=%d",
        len(namespace.set),
        len(namespace.set_file),
    )
    LOGGER.debug(
        "install lock resolution started origin_kind=%s frozen=%s cached=%s",
        origin.kind,
        bool(namespace.frozen),
        cached_lock is not None,
    )
    with activity(
        namespace,
        "Resolving sources and Docker images",
    ) as progress:
        lockfile = (
            validate_frozen_lock(
                cached_lock,
                manifest,
                cache(),
                recipe_origin=origin,
            )
            if cached_lock is not None
            else resolve_lock(
                root,
                manifest,
                frozen=namespace.frozen,
                recipe_origin=origin,
            )
        )
        LOGGER.debug(
            "install lock resolution completed sources=%d build=%s",
            len(lockfile.sources),
            lockfile.build is not None,
        )
        progress.update("Building and verifying the package")
        LOGGER.debug("install package build started")
        temporary, package = build_package(root, manifest, lockfile, values)
        LOGGER.debug(
            "install package build completed files=%d",
            len(package.metadata.files),
        )

    try:
        if origin.kind != "local" and cached_lock is None:
            recipe_cache().store(snapshot, lockfile, locator=cache_locator)
        installer = TransactionalInstaller(index=InstanceIndex(index_path()))
        with activity(namespace, "Planning the installation"):
            plan = installer.plan(package, target)
        LOGGER.debug(
            "install plan completed changes=%d conflicts=%s",
            len(plan.changes),
            plan.has_conflicts,
        )
        preview = Preview.for_install(snapshot, lockfile, plan, inputs=values)
        if plan.has_conflicts:
            if not namespace.json:
                print_human(preview.to_text(), tone="warning")
            conflicts = [
                change.path for change in plan.changes if change.action == "conflict"
            ]
            raise ConflictError(
                "install plan contains user-file conflicts",
                conflicts=conflicts,
                preview=preview.to_dict(),
            )
        if namespace.dry_run:
            if not namespace.json:
                print_human(preview.to_text(), tone="info")
            LOGGER.debug("install apply skipped dry_run=true")
            return _emit_result(namespace, preview, None)
        if confirm:
            LOGGER.debug("install confirmation stage entered")
            _confirm(namespace, preview)
        elif not namespace.json:
            print_human(preview.to_text(), tone="info")
        LOGGER.debug("install transaction apply started")
        with activity(namespace, "Applying the installation transaction"):
            plan, state = installer.install(
                manifest,
                lockfile,
                package,
                target,
                inputs=values,
                recipe_snapshot=snapshot,
            )
        LOGGER.debug("install transaction apply completed")
        return _emit_result(
            namespace,
            Preview.for_install(snapshot, lockfile, plan, inputs=values),
            state,
        )
    finally:
        temporary.cleanup()
        LOGGER.debug("install package workspace cleaned")


def _confirm(
    namespace: Any,
    preview: Preview,
) -> None:
    if not namespace.json:
        print_human(preview.to_text(), tone="info")
    if namespace.yes:
        return
    if namespace.non_interactive or namespace.json:
        raise ConflictError("install requires --yes in non-interactive mode")
    from luminesk_cli.cli.output import confirm

    if not confirm("Continue?"):
        raise ConflictError("installation was not confirmed")


def _emit_result(namespace: Any, preview: Preview, state: Any) -> int:
    plan = preview.plan
    payload = {
        "operation": plan.operation,
        "target": plan.target,
        "dryRun": state is None,
        "instanceId": state.instance_id if state is not None else None,
        "changes": [
            {
                "action": item.action,
                "path": item.path,
                "reason": item.reason,
                "digest": item.digest,
            }
            for item in plan.changes
        ],
        "preview": preview.to_dict(),
    }
    verb = "Planned" if state is None else "Installed"
    emit(namespace, payload, f"{verb} {plan.target} ({len(plan.changes)} changes)")
    return 0
