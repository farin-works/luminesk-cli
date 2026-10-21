"""PaperMC Fill v3 resolver for stable Java server builds."""

from __future__ import annotations

import re
from typing import Any

import httpx

from luminesk_cli.domain.errors import ResolutionError
from luminesk_cli.domain.manifest import PaperOptions, SourceSpec
from luminesk_cli.domain.primitives import validate_digest, validate_https_url
from luminesk_cli.infrastructure.sources.base import Resolution
from luminesk_cli.infrastructure.sources.common import (
    request_json_object,
    request_metadata,
)

API_ROOT = "https://fill.papermc.io/v3/projects/paper"
HEADERS = {"User-Agent": "luminesk/2.0 (https://github.com/farin-works/luminesk-cli)"}


class PaperResolver:
    def resolve(self, source: SourceSpec, client: httpx.Client) -> Resolution:
        if not isinstance(source.options, PaperOptions):
            raise ResolutionError("paper source has invalid options")

        options = source.options
        minecraft = options.minecraft
        if minecraft in {"latest", "*"} or "x" in minecraft.lower():
            project = request_json_object(
                client,
                API_ROOT,
                source,
                headers=HEADERS,
            )
            minecraft = _select_minecraft_version(project, minecraft)

        response = request_metadata(
            client,
            f"{API_ROOT}/versions/{minecraft}/builds",
            source,
            headers=HEADERS,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ResolutionError("Paper builds metadata is not valid JSON") from exc
        if not isinstance(payload, list):
            raise ResolutionError("Paper builds metadata must be an array")
        build = _select_build(payload, options.build)
        build_id = build.get("id")
        if not isinstance(build_id, int) or isinstance(build_id, bool) or build_id < 1:
            raise ResolutionError("Paper build has no valid id")
        downloads = build.get("downloads")
        download = (
            downloads.get("server:default") if isinstance(downloads, dict) else None
        )
        if not isinstance(download, dict):
            raise ResolutionError("Paper build has no server:default download")
        url = download.get("url")
        size = download.get("size")
        checksums = download.get("checksums")
        sha256 = checksums.get("sha256") if isinstance(checksums, dict) else None
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ResolutionError("Paper server download has invalid size")
        digest = validate_digest(
            f"sha256:{sha256}",
            "paper.download.checksums.sha256",
        )

        return Resolution(
            type=source.type,
            version=f"{minecraft}-{build_id}",
            source_revision=f"paper:{minecraft}:{build_id}",
            url=validate_https_url(url, "paper.download.url"),
            target=source.target,
            size=size,
            digest=digest,
            media_type="application/java-archive",
        )


def _select_minecraft_version(project: dict[str, Any], selector: str) -> str:
    grouped = project.get("versions")
    if not isinstance(grouped, dict):
        raise ResolutionError("Paper project versions must be an object")
    versions = [
        version
        for group in grouped.values()
        if isinstance(group, list)
        for version in group
        if isinstance(version, str)
    ]
    if selector not in {"latest", "*"}:
        prefix = selector.lower().removesuffix(".x") + "."
        versions = [version for version in versions if version.startswith(prefix)]
    if not versions:
        raise ResolutionError(f"Paper has no Minecraft version matching {selector}")
    return max(versions, key=_version_key)


def _version_key(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value)
    if match is None:
        return ()
    return tuple(int(part) for part in value.split("."))


def _select_build(builds: list[Any], selector: str | int) -> dict[str, Any]:
    candidates = [item for item in builds if isinstance(item, dict)]
    if selector == "latest":
        candidates = [item for item in candidates if item.get("channel") == "STABLE"]
        if not candidates:
            raise ResolutionError("Paper has no stable build for the selected version")
        return max(candidates, key=_build_id)

    try:
        build_id = int(selector)
    except (TypeError, ValueError) as exc:
        raise ResolutionError("Paper build must be latest or an integer") from exc
    matches = [item for item in candidates if item.get("id") == build_id]
    if len(matches) != 1:
        raise ResolutionError(f"Paper build does not exist: {build_id}")
    return matches[0]


def _build_id(item: dict[str, Any]) -> int:
    value = item.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1
