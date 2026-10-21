"""GitLab release asset resolver."""

from __future__ import annotations

import fnmatch
import os
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from luminesk_cli.domain.errors import ResolutionError
from luminesk_cli.domain.manifest import GitLabReleaseOptions, SourceSpec
from luminesk_cli.domain.primitives import validate_https_url
from luminesk_cli.infrastructure.sources.base import Resolution
from luminesk_cli.infrastructure.sources.common import request_json_object


class GitLabReleaseResolver:
    def resolve(self, source: SourceSpec, client: httpx.Client) -> Resolution:
        if not isinstance(source.options, GitLabReleaseOptions):
            raise ResolutionError("gitlab-release source has invalid options")

        options = source.options
        headers = _headers()
        project = quote(options.project, safe="")
        api_root = f"{options.base_url.rstrip('/')}/api/v4/projects/{project}/releases"
        selector = (
            "permalink/latest"
            if options.version in {"latest", "*"}
            else quote(options.version, safe="")
        )
        release = request_json_object(
            client,
            f"{api_root}/{selector}",
            source,
            headers=headers,
        )
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag:
            raise ResolutionError("GitLab release has no tag_name")

        asset = _select_asset(release, options.asset)
        raw_url = asset.get("direct_asset_url") or asset.get("url")
        if not isinstance(raw_url, str) or not raw_url:
            raise ResolutionError("GitLab release asset has no URL")
        url = urljoin(f"{options.base_url.rstrip('/')}/", raw_url)
        commit = release.get("commit")
        revision = commit.get("id") if isinstance(commit, dict) else None
        if not isinstance(revision, str) or not revision:
            revision = tag

        return Resolution(
            type=source.type,
            version=tag.lstrip("v"),
            source_revision=revision,
            url=validate_https_url(
                url, "gitlab.release.asset.url", allow_http=source.allow_http
            ),
            target=source.target,
        )


def _headers() -> dict[str, str]:
    headers = {
        "User-Agent": "luminesk/2.0 (https://github.com/farin-works/luminesk-cli)"
    }
    token = os.environ.get("GITLAB_TOKEN")
    if token:
        headers["PRIVATE-TOKEN"] = token
    return headers


def _select_asset(release: dict[str, Any], pattern: str) -> dict[str, Any]:
    assets = release.get("assets")
    links = assets.get("links") if isinstance(assets, dict) else None
    if not isinstance(links, list):
        raise ResolutionError("GitLab release assets.links must be an array")

    matches = [
        item
        for item in links
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and fnmatch.fnmatch(item["name"], pattern)
    ]
    if not matches:
        raise ResolutionError(f"no GitLab release asset matches {pattern}")
    if len(matches) != 1:
        raise ResolutionError(
            f"GitLab release asset pattern {pattern} is ambiguous",
            count=len(matches),
        )
    return matches[0]
