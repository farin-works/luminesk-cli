from __future__ import annotations

import difflib
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from luminesk_cli.application.install import restore_install_backup
from luminesk_cli.application.update import UpdateResult, UpdateService
from luminesk_cli.cli.commands.common import (
    build_package,
    cache,
    catalog_store,
    emit,
    recipe_cache,
    resolve_lock,
    validate_frozen_lock,
)
from luminesk_cli.cli.commands.runtime import _instance_root
from luminesk_cli.cli.input_wizard import collect_recipe_inputs
from luminesk_cli.cli.output import print_human
from luminesk_cli.cli.progress import activity
from luminesk_cli.domain.catalog import CatalogEntry
from luminesk_cli.domain.errors import ConflictError, TransactionError, ValidationError
from luminesk_cli.domain.lockfile import (
    LOCKFILE_NAME,
    Lockfile,
    RecipeLock,
    load_lockfile,
)
from luminesk_cli.domain.manifest import MANIFEST_NAME, Manifest, load_manifest
from luminesk_cli.domain.recipe import RecipeOrigin, RecipeSnapshot
from luminesk_cli.infrastructure.cache import digest_file
from luminesk_cli.infrastructure.catalog import CatalogClient
from luminesk_cli.infrastructure.oci import OciImageResolver
from luminesk_cli.infrastructure.recipe import (
    acquire_github_recipe,
    normalize_git_source,
)
from luminesk_cli.infrastructure.recipe_cache import database_locator, github_locator
from luminesk_cli.infrastructure.recipe_snapshot import (
    create_recipe_snapshot,
    load_verified_installed_recipe,
)
from luminesk_cli.infrastructure.state import (
    RECIPE_DIRECTORY,
    load_ownership,
    load_state,
    state_directory,
)

MAX_TEXT_DIFF_SIZE = 512 * 1024
RECIPE_VERSION_WARNING = "Recipe content changed without a package.version bump."


def run(namespace: Any) -> int:
    root = _instance_root(namespace.dir)
    old_lock = load_lockfile(root / LOCKFILE_NAME)
    installed = load_verified_installed_recipe(root, old_lock)

    with tempfile.TemporaryDirectory(prefix="luminesk-update-recipe-") as temporary:
        if namespace.frozen:
            candidate = _frozen_candidate(old_lock, installed)
        else:
            with activity(namespace, "Checking and verifying the recipe update"):
                candidate = _candidate_recipe(
                    old_lock,
                    installed,
                    Path(temporary) / "recipe",
                )
        manifest = candidate.manifest
        values = _update_inputs(
            root,
            manifest,
            namespace.set,
            namespace.set_file,
            interactive=not namespace.non_interactive and not namespace.json,
        )
        with activity(
            namespace,
            "Resolving sources and Docker images",
        ):
            OciImageResolver().require_target(old_lock.target)
            new_lock = (
                validate_frozen_lock(
                    old_lock,
                    manifest,
                    cache(),
                    recipe_origin=candidate.origin,
                )
                if namespace.frozen
                else resolve_lock(
                    candidate.root,
                    manifest,
                    frozen=False,
                    recipe_origin=candidate.origin,
                    target=old_lock.target,
                )
            )
        new_lock = _select_component(namespace.component, old_lock, new_lock)
        with activity(namespace, "Building and verifying the package"):
            temporary_package, package = build_package(
                candidate.root,
                manifest,
                new_lock,
                values,
            )

        try:
            if not namespace.frozen and candidate.origin.kind != "local":
                recipe_cache().store(
                    candidate,
                    new_lock,
                    locator=_candidate_locator(candidate.origin),
                )
            service = UpdateService()
            with activity(namespace, "Planning the update transaction"):
                preview = service.update(
                    root,
                    manifest,
                    new_lock,
                    package,
                    inputs=values,
                    recipe_snapshot=candidate,
                    dry_run=True,
                )
            recipe_changes = _snapshot_diff(installed, candidate)
            security_changes = _security_changes(installed.manifest, manifest)
            warnings = _recipe_warnings(old_lock, new_lock)

            if not namespace.dry_run:
                _confirm_update(
                    namespace,
                    root,
                    old_lock,
                    new_lock,
                    manifest,
                    security_changes,
                    warnings,
                )

            if namespace.dry_run:
                result = preview
            else:
                with activity(namespace, "Applying the update transaction"):
                    result = service.update(
                        root,
                        manifest,
                        new_lock,
                        package,
                        inputs=values,
                        recipe_snapshot=candidate,
                    )
        finally:
            temporary_package.cleanup()

    package_changes = _result_changes(result)
    emit(
        namespace,
        {
            "dryRun": namespace.dry_run,
            "recipeChanges": recipe_changes,
            "packageChanges": package_changes,
            "changes": package_changes,
            "securitySensitiveChanges": security_changes,
            "warnings": warnings,
            "instanceId": result.state.instance_id if result.state else None,
        },
        ("Planned" if namespace.dry_run else "Updated")
        + f" {root} ({len(recipe_changes) + len(package_changes)} changes)"
        + (
            "\n" + "\n".join(f"Warning: {warning}" for warning in warnings)
            if namespace.dry_run and warnings
            else ""
        ),
    )
    return 0


def outdated(namespace: Any) -> int:
    root = _instance_root(namespace.dir)
    old_lock = load_lockfile(root / LOCKFILE_NAME)
    installed = load_verified_installed_recipe(root, old_lock)

    with tempfile.TemporaryDirectory(prefix="luminesk-outdated-") as temporary:
        with activity(namespace, "Checking recipe, source, and image updates"):
            OciImageResolver().require_target(old_lock.target)
            candidate = _candidate_recipe(
                old_lock,
                installed,
                Path(temporary) / "recipe",
            )
            new_lock = resolve_lock(
                candidate.root,
                candidate.manifest,
                frozen=False,
                recipe_origin=candidate.origin,
                target=old_lock.target,
            )

    updates = _lock_changes(old_lock, new_lock)
    warnings = _recipe_warnings(old_lock, new_lock)
    lines = [
        f"  {item['component']}: {item['from']} -> {item['to']}" for item in updates
    ]
    emit(
        namespace,
        {"outdated": updates, "count": len(updates), "warnings": warnings},
        "No updates available."
        if not updates
        else f"{candidate.origin.entry or candidate.manifest.package.name}\n"
        + "\n".join(lines)
        + (
            "\n" + "\n".join(f"  warning: {warning}" for warning in warnings)
            if warnings
            else ""
        ),
        tone="info",
    )
    return 0


def diff(namespace: Any) -> int:
    root = _instance_root(namespace.dir)
    lockfile = load_lockfile(root / LOCKFILE_NAME)
    installed = _installed_recipe(root, lockfile, verify=False)
    recipe_drift = _recipe_drift(root, lockfile, installed)
    managed_drift = _managed_drift(root)

    if namespace.offline:
        upstream_diff = None
    else:
        with tempfile.TemporaryDirectory(prefix="luminesk-diff-") as temporary:
            with activity(namespace, "Checking the upstream recipe"):
                candidate = _candidate_recipe(
                    lockfile,
                    installed,
                    Path(temporary) / "recipe",
                )
            upstream_diff = _snapshot_diff(installed, candidate)

    sections = [
        _plain_drift_section("Recipe drift", recipe_drift),
        (
            "Template/recipe upstream diff\n  skipped (--offline)"
            if upstream_diff is None
            else _plain_diff_section("Template/recipe upstream diff", upstream_diff)
        ),
        _plain_drift_section("Managed instance file drift", managed_drift),
    ]
    emit(
        namespace,
        {
            "recipeDrift": recipe_drift,
            "upstreamRecipeDiff": upstream_diff,
            "upstreamChecked": upstream_diff is not None,
            "managedFileDrift": managed_drift,
        },
        "\n\n".join(sections),
        tone="info",
    )
    return 0


def recover(namespace: Any) -> int:
    root = _instance_root(namespace.dir)
    local_state = state_directory(root)
    journal = local_state / "transaction.json"
    backups = local_state / "backups"
    transaction_id = None
    force_clean = bool(getattr(namespace, "force_clean", False))

    if journal.exists() or journal.is_symlink():
        if journal.is_symlink() or not journal.is_file():
            raise TransactionError("transaction journal is invalid")
        try:
            transaction_id = json.loads(journal.read_text(encoding="utf-8"))["id"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise TransactionError("transaction journal is invalid") from exc
        if not isinstance(transaction_id, str) or not transaction_id:
            raise TransactionError("transaction journal is invalid")

    if transaction_id is not None:
        backup = backups / transaction_id
    else:
        if not force_clean:
            raise TransactionError(
                "no active transaction journal; refusing to restore a retained backup"
            )
        candidates = sorted(
            (path for path in backups.iterdir() if path.is_dir())
            if backups.exists()
            else (),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise TransactionError("no recoverable transaction was found")
        backup = candidates[0]
        transaction_id = backup.name

    if force_clean:
        _confirm_forced_recovery(namespace, root, backup)

    with activity(namespace, "Restoring the instance transaction backup"):
        restore_install_backup(
            root,
            backup,
            transaction_id=transaction_id,
            allow_completed=force_clean,
        )
    journal.unlink(missing_ok=True)
    emit(namespace, {"backup": str(backup)}, f"Recovered instance from {backup}")
    return 0


def _confirm_forced_recovery(namespace: Any, root: Path, backup: Path) -> None:
    if namespace.yes:
        return
    if namespace.non_interactive or namespace.json:
        raise ConflictError("forced recovery requires --yes in non-interactive mode")
    from luminesk_cli.cli.output import confirm

    if not confirm(
        f"Force recovery of {root} from {backup}? Current files may be removed."
    ):
        raise ConflictError("forced recovery was not confirmed")


def _origin(recipe: RecipeLock) -> RecipeOrigin:
    return RecipeOrigin(
        kind=recipe.kind,
        source=recipe.source,
        revision=recipe.revision,
        ref=recipe.ref,
        tracking=recipe.tracking,
        entry=recipe.entry,
        path=recipe.path,
        version=recipe.version,
        manifest_digest=recipe.manifest_digest,
        template_digest=recipe.template_digest,
    )


def _installed_recipe(
    root: Path,
    lockfile: Lockfile,
    *,
    verify: bool,
) -> RecipeSnapshot:
    if lockfile.recipe is None:
        raise ValidationError(
            "instance lock has no recipe origin; reinstall it with current Luminesk"
        )
    origin = _origin(lockfile.recipe)
    canonical = state_directory(root) / RECIPE_DIRECTORY
    snapshot = create_recipe_snapshot(
        canonical,
        load_manifest(canonical / MANIFEST_NAME),
        kind=origin.kind,
        source=origin.source,
        revision=origin.revision,
        ref=origin.ref,
        tracking=origin.tracking,
        entry=origin.entry,
        path=origin.path,
    )
    if verify and (
        snapshot.origin.manifest_digest != origin.manifest_digest
        or snapshot.origin.template_digest != origin.template_digest
        or snapshot.origin.version != origin.version
    ):
        raise ValidationError(
            "canonical installed recipe has drift; inspect it with `nesk diff`"
        )
    return snapshot


def _candidate_recipe(
    lockfile: Lockfile,
    installed: RecipeSnapshot,
    destination: Path,
) -> RecipeSnapshot:
    recipe = lockfile.recipe
    assert recipe is not None
    if recipe.kind == "local":
        return installed

    if not recipe.tracking:
        if recipe.kind != "github" or installed.manifest.build is None:
            return installed
        source = normalize_git_source(recipe.source, recipe.revision)
        fetched = acquire_github_recipe(source, destination, cache())
        if (
            fetched.origin.revision != recipe.revision
            or fetched.origin.manifest_digest != recipe.manifest_digest
            or fetched.origin.template_digest != recipe.template_digest
            or fetched.origin.version != recipe.version
        ):
            raise ValidationError(
                "pinned GitHub recipe content no longer matches its lock"
            )
        return RecipeSnapshot(
            root=fetched.root,
            manifest=fetched.manifest,
            origin=_origin(recipe),
            entries=fetched.entries,
        )

    if recipe.kind == "database":
        if recipe.entry is None:
            raise ValidationError("database recipe origin has no entry name")
        store = catalog_store()
        catalog = store.load_active()
        entry = next(
            (item for item in catalog.entries if item.name == recipe.entry),
            None,
        )
        if entry is None:
            raise ValidationError(
                f"active catalog has no installed recipe: {recipe.entry}"
            )
        if _database_entry_matches_lock(recipe, entry):
            return installed
        return CatalogClient(store).acquire_entry(catalog, entry, destination)

    if recipe.kind == "github":
        if recipe.ref is None:
            raise ValidationError("tracking GitHub recipe origin has no branch ref")
        source = normalize_git_source(recipe.source, recipe.ref)
        candidate = acquire_github_recipe(source, destination, cache())
        if not candidate.origin.tracking or candidate.origin.ref != recipe.ref:
            raise ValidationError(
                "tracked GitHub branch no longer resolves as a branch"
            )
        return candidate

    raise ValidationError(f"unsupported recipe origin kind: {recipe.kind}")


def _database_entry_matches_lock(
    recipe: RecipeLock,
    entry: CatalogEntry,
) -> bool:
    """Return whether an active entry is the exact installed database recipe."""

    return (
        recipe.entry == entry.name
        and recipe.path == entry.path
        and recipe.version == entry.recipe_version
        and recipe.manifest_digest == entry.manifest_digest
        and recipe.template_digest == entry.template_digest
    )


def _frozen_candidate(
    lockfile: Lockfile,
    installed: RecipeSnapshot,
) -> RecipeSnapshot:
    if installed.origin.kind == "local":
        return installed
    try:
        return recipe_cache().load_exact(installed.origin, lockfile).snapshot
    except ValidationError:
        if installed.manifest.build is not None:
            raise ValidationError(
                "frozen GitHub build context is absent from recipe cache"
            ) from None
        return installed


def _candidate_locator(origin: RecipeOrigin) -> str:
    if origin.kind == "database":
        assert origin.entry is not None
        return database_locator(origin.revision, origin.entry)
    if origin.kind == "github":
        return github_locator(origin.source, origin.ref)
    raise ValidationError("local recipe does not have a remote cache locator")


def _select_component(
    component: str | None,
    old: Lockfile,
    new: Lockfile,
) -> Lockfile:
    if component is None or component == "recipe":
        return new
    if component == "runtime":
        return Lockfile(
            manifest_digest=new.manifest_digest,
            target=new.target,
            sources=old.sources,
            runtime=new.runtime,
            build=new.build,
            recipe=new.recipe,
        )
    if component not in new.sources:
        raise ValidationError(f"unknown update component: {component}")
    return Lockfile(
        manifest_digest=new.manifest_digest,
        target=new.target,
        sources={**old.sources, component: new.sources[component]},
        runtime=old.runtime,
        build=new.build,
        recipe=new.recipe,
    )


def _lock_changes(old: Lockfile, new: Lockfile) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    if old.recipe is not None and new.recipe is not None:
        if old.recipe.version != new.recipe.version:
            changes.append(
                {
                    "component": "recipe version",
                    "from": old.recipe.version,
                    "to": new.recipe.version,
                }
            )
        if old.recipe.revision != new.recipe.revision:
            changes.append(
                {
                    "component": "recipe revision",
                    "from": old.recipe.revision,
                    "to": new.recipe.revision,
                }
            )
        if (
            old.recipe.manifest_digest != new.recipe.manifest_digest
            or old.recipe.template_digest != new.recipe.template_digest
        ) and old.recipe.version == new.recipe.version:
            changes.append(
                {
                    "component": "recipe content",
                    "from": (
                        f"{old.recipe.manifest_digest} "
                        f"{old.recipe.template_digest or 'none'}"
                    ),
                    "to": (
                        f"{new.recipe.manifest_digest} "
                        f"{new.recipe.template_digest or 'none'}"
                    ),
                }
            )

    for source_id, source in sorted(new.sources.items()):
        previous = old.sources.get(source_id)
        if previous is None or (
            previous.version,
            previous.digest,
        ) != (source.version, source.digest):
            before = f"{previous.version} {previous.digest}" if previous else "absent"
            changes.append(
                {
                    "component": source_id,
                    "from": before,
                    "to": f"{source.version} {source.digest}",
                }
            )
    for source_id in sorted(set(old.sources) - set(new.sources)):
        previous = old.sources[source_id]
        changes.append(
            {
                "component": source_id,
                "from": f"{previous.version} {previous.digest}",
                "to": "absent",
            }
        )
    if old.runtime.image != new.runtime.image:
        changes.append(
            {"component": "runtime", "from": old.runtime.image, "to": new.runtime.image}
        )
    return changes


def _recipe_warnings(old: Lockfile, new: Lockfile) -> list[str]:
    before = old.recipe
    after = new.recipe
    if before is None or after is None:
        return []
    content_changed = (
        before.manifest_digest != after.manifest_digest
        or before.template_digest != after.template_digest
    )
    if (
        before.kind == after.kind == "github"
        and content_changed
        and before.version == after.version
    ):
        return [RECIPE_VERSION_WARNING]
    return []


def _result_changes(result: UpdateResult) -> list[dict[str, str]]:
    return [
        {"action": change.action, "path": change.path, "reason": change.reason}
        for change in result.install_plan.changes
        if change.action != "preserve"
    ]


def _snapshot_diff(
    installed: RecipeSnapshot,
    candidate: RecipeSnapshot,
) -> list[dict[str, Any]]:
    old_entries = {entry.path: entry for entry in installed.entries}
    new_entries = {entry.path: entry for entry in candidate.entries}
    changes: list[dict[str, Any]] = []
    for relative in sorted(set(old_entries) | set(new_entries)):
        old = old_entries.get(relative)
        new = new_entries.get(relative)
        if old is None:
            changes.append({"path": relative, "status": "added", "diff": []})
            continue
        if new is None:
            changes.append({"path": relative, "status": "removed", "diff": []})
            continue
        if (old.type, old.mode, old.digest) == (new.type, new.mode, new.digest):
            continue
        lines: list[str] = []
        if old.type == new.type == "file":
            lines = _text_diff(
                installed.root / relative,
                candidate.root / relative,
                relative,
            )
        changes.append({"path": relative, "status": "changed", "diff": lines})
    return changes


def _text_diff(old: Path, new: Path, relative: str) -> list[str]:
    if (
        old.stat().st_size > MAX_TEXT_DIFF_SIZE
        or new.stat().st_size > MAX_TEXT_DIFF_SIZE
    ):
        return []
    try:
        old_lines = old.read_text(encoding="utf-8").splitlines()
        new_lines = new.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    return list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"installed/{relative}",
            tofile=f"upstream/{relative}",
            lineterm="",
        )
    )


def _recipe_drift(
    root: Path,
    lockfile: Lockfile,
    snapshot: RecipeSnapshot,
) -> list[dict[str, str]]:
    recipe = lockfile.recipe
    assert recipe is not None
    drift = []
    if snapshot.origin.manifest_digest != recipe.manifest_digest:
        drift.append(
            {"path": ".luminesk_cli/recipe/luminesk.toml", "status": "modified"}
        )
    if snapshot.origin.template_digest != recipe.template_digest:
        drift.append({"path": ".luminesk_cli/recipe/<template>", "status": "modified"})
    root_manifest = root / "luminesk.toml"
    canonical_manifest = snapshot.root / "luminesk.toml"
    if not root_manifest.is_file() or root_manifest.is_symlink():
        drift.append({"path": "luminesk.toml", "status": "missing"})
    else:
        root_digest, _ = digest_file(root_manifest)
        canonical_digest, _ = digest_file(canonical_manifest)
        if root_digest != canonical_digest:
            drift.append({"path": "luminesk.toml", "status": "modified"})
    return drift


def _managed_drift(root: Path) -> list[dict[str, str]]:
    changes = []
    for relative, entry in load_ownership(root).files.items():
        if entry.mode not in {"managed", "generated"} or entry.digest is None:
            continue
        path = root / relative
        if not path.is_file() or path.is_symlink():
            changes.append({"path": relative, "status": "missing"})
            continue
        digest_value, _ = digest_file(path)
        if digest_value != entry.digest:
            changes.append({"path": relative, "status": "modified"})
    return changes


def _security_changes(old: Manifest, new: Manifest) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    old_sources = {source.id: source for source in old.sources}
    new_sources = {source.id: source for source in new.sources}
    for source_id in sorted(set(old_sources) | set(new_sources)):
        before = old_sources.get(source_id)
        after = new_sources.get(source_id)
        if before is None or after is None:
            changes.append(
                {
                    "field": f"sources.{source_id}",
                    "from": before.type if before else "absent",
                    "to": after.type if after else "absent",
                }
            )
            continue
        if before.type != after.type:
            changes.append(
                {
                    "field": f"sources.{source_id}.type",
                    "from": before.type,
                    "to": after.type,
                }
            )
        before_options = asdict(before.options)
        after_options = asdict(after.options)
        for key in ("url", "repository", "base_url", "project"):
            old_value = before_options.get(key)
            new_value = after_options.get(key)
            if old_value != new_value:
                changes.append(
                    {
                        "field": f"sources.{source_id}.{key}",
                        "from": str(old_value) if old_value is not None else "absent",
                        "to": str(new_value) if new_value is not None else "absent",
                    }
                )
    old_image = _image_repository(old.runtime.image)
    new_image = _image_repository(new.runtime.image)
    if old_image != new_image:
        changes.append(
            {"field": "runtime.imageRepository", "from": old_image, "to": new_image}
        )
    return changes


def _image_repository(image: str) -> str:
    unpinned = image.split("@", 1)[0]
    parent, separator, name = unpinned.rpartition("/")
    repository_name = name.rsplit(":", 1)[0]
    return f"{parent}/{repository_name}" if separator else repository_name


def _update_inputs(
    root: Path,
    manifest: Manifest,
    arguments: list[str],
    file_arguments: list[str],
    *,
    interactive: bool,
) -> dict[str, str | int | bool]:
    state = load_state(root)
    known = {spec.name for spec in manifest.inputs}
    values = (
        {name: value for name, value in state.inputs.items() if name in known}
        if state is not None
        else {}
    )
    return collect_recipe_inputs(
        manifest,
        arguments,
        file_arguments,
        interactive=interactive,
        known_values=values,
    )


def _plain_drift_section(title: str, changes: list[dict[str, str]]) -> str:
    if not changes:
        return f"{title}\n  none"
    return (
        title
        + "\n"
        + "\n".join(f"  {item['status']:8} {item['path']}" for item in changes)
    )


def _plain_diff_section(title: str, changes: list[dict[str, Any]]) -> str:
    if not changes:
        return f"{title}\n  none"
    lines = [title]
    for item in changes:
        lines.append(f"  {item['status']:8} {item['path']}")
        lines.extend(f"    {line}" for line in item["diff"])
    return "\n".join(lines)


def _confirm_update(
    namespace: Any,
    root: Path,
    old_lock: Lockfile,
    new_lock: Lockfile,
    manifest: Manifest,
    security_changes: list[dict[str, str]],
    warnings: list[str],
) -> None:
    changes = _lock_changes(old_lock, new_lock)
    if not namespace.json:
        lines = [
            f"Update target: {root}",
            "Capabilities: "
            f"build={'enabled' if manifest.build else 'disabled'}, "
            f"build-network={'enabled' if manifest.build and manifest.build.network else 'disabled'}, "
            f"runtime={new_lock.runtime.image}",
        ]
        for change in changes:
            lines.append(f"  {change['component']}: {change['from']} -> {change['to']}")
        for change in security_changes:
            lines.append(f"Security-sensitive source change: {change['field']}")
            lines.append(f"  - {change['from']}")
            lines.append(f"  + {change['to']}")
        for warning in warnings:
            lines.append(f"Warning: {warning}")
        print_human("\n".join(lines), tone="info")
    if namespace.yes:
        return
    if namespace.non_interactive or namespace.json:
        raise ConflictError("update requires --yes in non-interactive mode")
    from luminesk_cli.cli.output import confirm

    if not confirm("Apply this update?"):
        raise ConflictError("update was not confirmed")
