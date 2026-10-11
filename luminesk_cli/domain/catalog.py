"""Strict domain model for the official luminesk-database index."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, Literal, cast

from luminesk_cli.domain.errors import ValidationError
from luminesk_cli.domain.primitives import (
    PACKAGE_NAME_RE,
    PLATFORM_RE,
    SEMVER_RE,
    reject_unknown,
    require_array,
    require_int,
    require_keys,
    require_string,
    require_table,
    safe_relative_path,
    sha256_digest,
    validate_digest,
    validate_https_url,
    validate_pinned_image,
)

INDEX_VERSION = 1
MAX_CATALOG_SIZE = 4 * 1024 * 1024
MAX_CATALOG_ENTRIES = 10_000
DATABASE_REPOSITORY = "github:task-v1/luminesk-database"
GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(slots=True, frozen=True)
class CatalogEntry:
    name: str
    display_name: str
    recipe_version: str
    kind: Literal["core", "template"]
    game: Literal["minecraft"]
    edition: Literal["java", "bedrock", "cross-platform"]
    summary: str
    keywords: tuple[str, ...]
    path: str
    manifest_digest: str
    template_digest: str | None = None
    license: str | None = None
    authors: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    repository: str | None = None
    source_types: tuple[str, ...] = ()
    runtime_image: str | None = None


@dataclass(slots=True, frozen=True)
class CatalogSnapshot:
    revision: str
    entries: tuple[CatalogEntry, ...]
    index_digest: str
    repository: str = DATABASE_REPOSITORY
    index_version: int = INDEX_VERSION


def parse_catalog_index(
    content: bytes,
    *,
    source: str = "dist/index-v1.json",
) -> CatalogSnapshot:
    if len(content) > MAX_CATALOG_SIZE:
        raise ValidationError(f"{source} exceeds the catalog index size limit")
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid catalog index: {exc}") from exc

    table = require_table(raw, "catalog")
    reject_unknown(table, {"indexVersion", "revision", "entries"}, "catalog")
    require_keys(table, {"indexVersion", "revision", "entries"}, "catalog")
    version = require_int(table["indexVersion"], "catalog.indexVersion")
    if version != INDEX_VERSION:
        raise ValidationError(
            f"unsupported catalog index version {version}; expected {INDEX_VERSION}"
        )
    revision = require_string(table["revision"], "catalog.revision")
    if GIT_REVISION_RE.fullmatch(revision) is None:
        raise ValidationError("catalog.revision must be a lowercase Git commit SHA")

    raw_entries = require_array(table["entries"], "catalog.entries")
    if len(raw_entries) > MAX_CATALOG_ENTRIES:
        raise ValidationError("catalog contains too many entries")
    entries = tuple(
        _parse_entry(value, index) for index, value in enumerate(raw_entries)
    )
    normalized: set[str] = set()
    for entry in entries:
        name = entry.name.casefold()
        if name in normalized:
            raise ValidationError(f"duplicate normalized catalog entry: {entry.name}")
        normalized.add(name)
    if [entry.name for entry in entries] != sorted(entry.name for entry in entries):
        raise ValidationError("catalog entries must be sorted by name")

    return CatalogSnapshot(
        revision=revision,
        entries=entries,
        index_digest=sha256_digest(content),
    )


def _parse_entry(value: Any, index: int) -> CatalogEntry:
    path = f"catalog.entries[{index}]"
    table = require_table(value, path)
    allowed = {
        "name",
        "displayName",
        "recipeVersion",
        "kind",
        "game",
        "edition",
        "summary",
        "keywords",
        "path",
        "manifestDigest",
        "templateDigest",
        "license",
        "authors",
        "platforms",
        "repository",
        "sourceTypes",
        "runtimeImage",
    }
    reject_unknown(table, allowed, path)
    require_keys(
        table,
        allowed
        - {
            "templateDigest",
            "license",
            "authors",
            "platforms",
            "repository",
            "sourceTypes",
            "runtimeImage",
        },
        path,
    )
    name = _identifier(table["name"], f"{path}.name")
    entry_path = safe_relative_path(table["path"], f"{path}.path")
    if entry_path != f"database/{name}":
        raise ValidationError(f"{path}.path must equal database/<name>")
    recipe_version = require_string(table["recipeVersion"], f"{path}.recipeVersion")
    if SEMVER_RE.fullmatch(recipe_version) is None:
        raise ValidationError(f"{path}.recipeVersion must be semantic versioning")
    kind = require_string(table["kind"], f"{path}.kind")
    if kind not in {"core", "template"}:
        raise ValidationError(f"{path}.kind must be core or template")
    game = require_string(table["game"], f"{path}.game")
    if game != "minecraft":
        raise ValidationError(f"{path}.game must be minecraft")
    edition = require_string(table["edition"], f"{path}.edition")
    if edition not in {"java", "bedrock", "cross-platform"}:
        raise ValidationError(f"{path}.edition is unsupported")
    keywords = tuple(
        require_string(item, f"{path}.keywords[{keyword_index}]")
        for keyword_index, item in enumerate(
            require_array(table["keywords"], f"{path}.keywords")
        )
    )
    template_digest = table.get("templateDigest")
    if template_digest is not None:
        template_digest = validate_digest(template_digest, f"{path}.templateDigest")

    license_name = _optional_nonempty_string(table, "license", path)
    repository = _optional_nonempty_string(table, "repository", path)
    if repository is not None:
        repository = validate_https_url(repository, f"{path}.repository")
    runtime_image = _optional_nonempty_string(table, "runtimeImage", path)
    if runtime_image is not None:
        runtime_image = validate_pinned_image(runtime_image, f"{path}.runtimeImage")
    platforms = _optional_string_array(table, "platforms", path)
    for platform_index, platform in enumerate(platforms):
        if PLATFORM_RE.fullmatch(platform) is None:
            raise ValidationError(
                f"{path}.platforms[{platform_index}] must be os/architecture"
            )

    return CatalogEntry(
        name=name,
        display_name=require_string(table["displayName"], f"{path}.displayName"),
        recipe_version=recipe_version,
        kind=kind,
        game="minecraft",
        edition=edition,
        summary=require_string(table["summary"], f"{path}.summary"),
        keywords=keywords,
        path=entry_path,
        manifest_digest=validate_digest(
            table["manifestDigest"], f"{path}.manifestDigest"
        ),
        template_digest=template_digest,
        license=license_name,
        authors=_optional_string_array(table, "authors", path),
        platforms=platforms,
        repository=repository,
        source_types=_optional_string_array(table, "sourceTypes", path),
        runtime_image=runtime_image,
    )


def _optional_nonempty_string(table: dict[str, Any], key: str, path: str) -> str | None:
    if key not in table:
        return None
    return require_string(table[key], f"{path}.{key}")


def _optional_string_array(
    table: dict[str, Any], key: str, path: str
) -> tuple[str, ...]:
    if key not in table:
        return ()
    return tuple(
        require_string(item, f"{path}.{key}[{index}]")
        for index, item in enumerate(require_array(table[key], f"{path}.{key}"))
    )


def _identifier(value: Any, path: str) -> str:
    identifier = require_string(value, path)
    if not identifier.isascii() or PACKAGE_NAME_RE.fullmatch(identifier) is None:
        raise ValidationError(f"{path} must be a lowercase ASCII identifier")
    return identifier


def search_catalog(
    snapshot: CatalogSnapshot,
    query: str = "",
    *,
    kind: str | None = None,
    edition: str | None = None,
) -> tuple[CatalogEntry, ...]:
    needle = query.casefold().strip()
    matches = [
        entry
        for entry in snapshot.entries
        if (kind is None or entry.kind == kind)
        and (edition is None or entry.edition == edition)
        and (not needle or _search_score(entry, needle) is not None)
    ]
    return tuple(
        sorted(
            matches,
            key=lambda entry: (
                -(_search_score(entry, needle) or 0),
                entry.name,
            ),
        )
    )


def _search_score(entry: CatalogEntry, needle: str) -> int | None:
    if not needle:
        return 0
    tokens = tuple(token for token in needle.split() if token)
    scores = [_token_score(entry, token) for token in tokens]
    if any(score is None for score in scores):
        return None
    score = sum(cast(int, item) for item in scores)
    if entry.name.casefold() == needle:
        score += 100
    return score


def _token_score(entry: CatalogEntry, token: str) -> int | None:
    name = entry.name.casefold()
    if name == token:
        return 100
    if name.startswith(token):
        return 80
    if token in name:
        return 70
    if any(keyword.casefold() == token for keyword in entry.keywords):
        return 60
    if token in entry.display_name.casefold():
        return 50
    if any(token in value.casefold() for value in entry.source_types):
        return 40
    if any(token in value.casefold() for value in entry.platforms):
        return 30
    if token in entry.edition or token in entry.kind:
        return 25
    if token in entry.summary.casefold():
        return 20
    return None


def suggest_catalog(
    snapshot: CatalogSnapshot, query: str, *, limit: int = 3
) -> tuple[str, ...]:
    """Suggest canonical entry names for a misspelled offline query."""

    needle = query.casefold().strip()
    if not needle:
        return ()
    aliases: dict[str, str] = {}
    for entry in snapshot.entries:
        aliases[entry.name.casefold()] = entry.name
        aliases[entry.display_name.casefold()] = entry.name
        for keyword in entry.keywords:
            aliases.setdefault(keyword.casefold(), entry.name)
    suggestions = get_close_matches(needle, aliases, n=limit * 2, cutoff=0.5)
    names: list[str] = []
    for suggestion in suggestions:
        name = aliases[suggestion]
        if name not in names:
            names.append(name)
        if len(names) == limit:
            break
    return tuple(names)
