"""Canonical lockfile models, parser, and crash-safe writer."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from luminesk_cli.domain.catalog import DATABASE_REPOSITORY_ALIASES
from luminesk_cli.domain.errors import ValidationError
from luminesk_cli.domain.primitives import (
    SEMVER_RE,
    reject_unknown,
    require_int,
    require_keys,
    require_string,
    require_table,
    safe_relative_path,
    validate_digest,
    validate_https_url,
    validate_pinned_image,
)

LOCKFILE_NAME = "luminesk.lock"
LOCKFILE_VERSION = 1
MAX_LOCKFILE_SIZE = 4 * 1024 * 1024


@dataclass(slots=True, frozen=True)
class RecipeLock:
    kind: Literal["database", "github", "local"]
    source: str
    revision: str
    version: str
    manifest_digest: str
    ref: str | None = None
    tracking: bool = False
    entry: str | None = None
    path: str | None = None
    template_digest: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"database", "github", "local"}:
            raise ValidationError("lockfile.recipe.kind is invalid")
        require_string(self.source, "lockfile.recipe.source")
        require_string(self.revision, "lockfile.recipe.revision")
        if not isinstance(self.tracking, bool):
            raise ValidationError("lockfile.recipe.tracking must be a boolean")
        if SEMVER_RE.fullmatch(self.version) is None:
            raise ValidationError("lockfile.recipe.version must be semantic versioning")
        validate_digest(self.manifest_digest, "lockfile.recipe.manifestDigest")
        if self.template_digest is not None:
            validate_digest(self.template_digest, "lockfile.recipe.templateDigest")

        if self.kind == "database":
            if self.source not in DATABASE_REPOSITORY_ALIASES:
                raise ValidationError("database recipe source is not official")
            if re.fullmatch(r"[0-9a-f]{40}", self.revision) is None:
                raise ValidationError("database recipe revision must be a Git commit")
            if not self.tracking or self.entry is None or self.path is None:
                raise ValidationError(
                    "database recipe origin requires tracking, entry, and path"
                )
        elif self.kind == "github":
            if (
                re.fullmatch(r"github:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.source)
                is None
            ):
                raise ValidationError("GitHub recipe source must be canonical")
            if re.fullmatch(r"[0-9a-f]{40}", self.revision) is None:
                raise ValidationError("GitHub recipe revision must be a Git commit")
            if self.ref is None:
                raise ValidationError("GitHub recipe origin requires an exact ref")
        else:
            if self.source != "local" or self.tracking:
                raise ValidationError("local recipe origin must be untracked and local")
            if any(value is not None for value in (self.ref, self.entry, self.path)):
                raise ValidationError("local recipe origin has remote-only fields")

        if self.ref is not None:
            require_string(self.ref, "lockfile.recipe.ref")
        if self.entry is not None:
            safe_relative_path(self.entry, "lockfile.recipe.entry")
        if self.path is not None:
            safe_relative_path(self.path, "lockfile.recipe.path", allow_dot=True)


@dataclass(slots=True, frozen=True)
class ResolvedSource:
    type: str
    version: str
    source_revision: str
    url: str
    size: int
    digest: str
    target: str
    media_type: str | None = None


@dataclass(slots=True, frozen=True)
class RuntimeLock:
    image: str


@dataclass(slots=True, frozen=True)
class BuildLock:
    images: dict[str, str]


@dataclass(slots=True, frozen=True)
class Lockfile:
    manifest_digest: str
    target: str
    sources: dict[str, ResolvedSource]
    runtime: RuntimeLock
    build: BuildLock | None = None
    recipe: RecipeLock | None = None
    lockfile_version: int = LOCKFILE_VERSION

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "lockfileVersion": self.lockfile_version,
            "manifestDigest": self.manifest_digest,
            "target": self.target,
            "sources": {
                source_id: {
                    "type": source.type,
                    "version": source.version,
                    "sourceRevision": source.source_revision,
                    "url": source.url,
                    "size": source.size,
                    "digest": source.digest,
                    "target": source.target,
                    **(
                        {"mediaType": source.media_type}
                        if source.media_type is not None
                        else {}
                    ),
                }
                for source_id, source in self.sources.items()
            },
            "runtime": {"image": self.runtime.image},
        }

        if self.build is not None:
            result["build"] = {"images": dict(sorted(self.build.images.items()))}

        if self.recipe is not None:
            result["recipe"] = {
                "kind": self.recipe.kind,
                "source": self.recipe.source,
                "revision": self.recipe.revision,
                "entry": self.recipe.entry,
                "path": self.recipe.path,
                "ref": self.recipe.ref,
                "tracking": self.recipe.tracking,
                "version": self.recipe.version,
                "manifestDigest": self.recipe.manifest_digest,
                "templateDigest": self.recipe.template_digest,
            }

        return result

    def to_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        from luminesk_cli.domain.primitives import sha256_digest

        return sha256_digest(self.to_bytes())


def _parse_source(source_id: str, value: Any) -> ResolvedSource:
    path = f"sources.{source_id}"
    table = require_table(value, path)
    reject_unknown(
        table,
        {
            "type",
            "version",
            "sourceRevision",
            "url",
            "size",
            "digest",
            "target",
            "mediaType",
        },
        path,
    )
    require_keys(
        table,
        {"type", "version", "sourceRevision", "url", "size", "digest", "target"},
        path,
    )
    source_type = require_string(table["type"], f"{path}.type")
    raw_url = require_string(table["url"], f"{path}.url")

    if source_type == "local-file":
        if not raw_url.startswith("local:"):
            raise ValidationError(f"{path}.url: local-file URL must start with local:")

        safe_relative_path(raw_url.removeprefix("local:"), f"{path}.url")
        url = raw_url
    else:
        url = validate_https_url(raw_url, f"{path}.url")

    return ResolvedSource(
        type=source_type,
        version=require_string(table["version"], f"{path}.version"),
        source_revision=require_string(
            table["sourceRevision"], f"{path}.sourceRevision"
        ),
        url=url,
        size=require_int(table["size"], f"{path}.size", minimum=0),
        digest=validate_digest(table["digest"], f"{path}.digest"),
        target=safe_relative_path(table["target"], f"{path}.target", allow_dot=True),
        media_type=(
            require_string(table["mediaType"], f"{path}.mediaType")
            if "mediaType" in table
            else None
        ),
    )


def parse_lockfile(content: bytes, *, source: str = LOCKFILE_NAME) -> Lockfile:
    if len(content) > MAX_LOCKFILE_SIZE:
        raise ValidationError(
            f"{source} exceeds the {MAX_LOCKFILE_SIZE}-byte limit",
            path=source,
            size=len(content),
        )

    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid {source}: {exc}", path=source) from exc

    table = require_table(raw, "lockfile")
    reject_unknown(
        table,
        {
            "lockfileVersion",
            "manifestDigest",
            "recipe",
            "target",
            "sources",
            "runtime",
            "build",
        },
        "lockfile",
    )
    require_keys(
        table,
        {"lockfileVersion", "manifestDigest", "target", "sources", "runtime"},
        "lockfile",
    )
    version = require_int(table["lockfileVersion"], "lockfile.lockfileVersion")

    if version != LOCKFILE_VERSION:
        raise ValidationError(
            f"unsupported lockfile version {version}; expected {LOCKFILE_VERSION}"
        )

    sources_table = require_table(table["sources"], "lockfile.sources")
    sources = {
        source_id: _parse_source(source_id, source_value)
        for source_id, source_value in sources_table.items()
    }
    runtime_table = require_table(table["runtime"], "lockfile.runtime")
    reject_unknown(runtime_table, {"image"}, "lockfile.runtime")
    require_keys(runtime_table, {"image"}, "lockfile.runtime")
    image = validate_pinned_image(runtime_table["image"], "lockfile.runtime.image")

    recipe = None

    if "recipe" in table:
        recipe_table = require_table(table["recipe"], "lockfile.recipe")
        reject_unknown(
            recipe_table,
            {
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
            },
            "lockfile.recipe",
        )
        require_keys(
            recipe_table,
            {
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
            },
            "lockfile.recipe",
        )
        kind = require_string(recipe_table["kind"], "lockfile.recipe.kind")
        if kind not in {"database", "github", "local"}:
            raise ValidationError("lockfile.recipe.kind is invalid")
        source_value = require_string(recipe_table["source"], "lockfile.recipe.source")
        if Path(source_value).is_absolute() or source_value.startswith("file:"):
            raise ValidationError("lockfile.recipe.source must not be a local path")
        if source_value.startswith(("http://", "https://")):
            validate_https_url(source_value, "lockfile.recipe.source")
        if kind == "local" and source_value != "local":
            raise ValidationError("local recipe source must be local")
        ref = _nullable_string(recipe_table["ref"], "lockfile.recipe.ref")
        entry = _nullable_safe_path(recipe_table["entry"], "lockfile.recipe.entry")
        recipe_path = _nullable_safe_path(
            recipe_table["path"], "lockfile.recipe.path", allow_dot=True
        )
        template_digest = recipe_table["templateDigest"]
        if template_digest is not None:
            template_digest = validate_digest(
                template_digest, "lockfile.recipe.templateDigest"
            )
        tracking = recipe_table["tracking"]

        if not isinstance(tracking, bool):
            raise ValidationError("lockfile.recipe.tracking must be a boolean")

        recipe = RecipeLock(
            kind=kind,  # type: ignore[arg-type]
            source=source_value,
            revision=require_string(
                recipe_table["revision"], "lockfile.recipe.revision"
            ),
            version=require_string(recipe_table["version"], "lockfile.recipe.version"),
            manifest_digest=validate_digest(
                recipe_table["manifestDigest"], "lockfile.recipe.manifestDigest"
            ),
            ref=ref,
            tracking=tracking,
            entry=entry,
            path=recipe_path,
            template_digest=template_digest,
        )

    build = None

    if "build" in table:
        build_table = require_table(table["build"], "lockfile.build")
        reject_unknown(build_table, {"images"}, "lockfile.build")
        require_keys(build_table, {"images"}, "lockfile.build")
        images_table = require_table(build_table["images"], "lockfile.build.images")
        images = {}

        for original, pinned_value in images_table.items():
            pinned = validate_pinned_image(
                pinned_value,
                f"lockfile.build.images.{original}",
            )

            images[original] = pinned

        build = BuildLock(images=images)

    return Lockfile(
        manifest_digest=validate_digest(
            table["manifestDigest"], "lockfile.manifestDigest"
        ),
        target=require_string(table["target"], "lockfile.target"),
        sources=sources,
        runtime=RuntimeLock(image=image),
        build=build,
        recipe=recipe,
    )


def _nullable_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return require_string(value, path)


def _nullable_safe_path(
    value: Any,
    path: str,
    *,
    allow_dot: bool = False,
) -> str | None:
    if value is None:
        return None
    return safe_relative_path(value, path, allow_dot=allow_dot)


def load_lockfile(path: Path) -> Lockfile:
    if path.name != LOCKFILE_NAME:
        raise ValidationError(
            f"lockfile must be named exactly {LOCKFILE_NAME}", path=str(path)
        )

    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read {path}: {exc}", path=str(path)) from exc

    return parse_lockfile(content, source=str(path))


def write_lockfile(path: Path, lockfile: Lockfile) -> None:
    if path.name != LOCKFILE_NAME:
        raise ValidationError(
            f"lockfile must be named exactly {LOCKFILE_NAME}", path=str(path)
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(lockfile.to_bytes())
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
