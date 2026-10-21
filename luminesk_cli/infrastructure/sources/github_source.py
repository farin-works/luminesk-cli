"""GitHub source archives resolved to an immutable commit."""

from __future__ import annotations

import os
import re
from urllib.parse import quote

import httpx

from luminesk_cli.domain.errors import ResolutionError
from luminesk_cli.domain.manifest import GitHubSourceOptions, SourceSpec
from luminesk_cli.infrastructure.sources.base import Resolution
from luminesk_cli.infrastructure.sources.common import request_json_object


class GitHubSourceResolver:
    def resolve(self, source: SourceSpec, client: httpx.Client) -> Resolution:
        if not isinstance(source.options, GitHubSourceOptions):
            raise ResolutionError("github-source source has invalid options")

        owner, repository = source.options.repository.split("/", 1)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "luminesk/2.0 (https://github.com/farin-works/luminesk-cli)",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        api_root = f"https://api.github.com/repos/{owner}/{repository}"
        commit = request_json_object(
            client,
            f"{api_root}/commits/{quote(source.options.ref, safe='')}",
            source,
            headers=headers,
        )
        revision = commit.get("sha")
        if (
            not isinstance(revision, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", revision) is None
        ):
            raise ResolutionError("GitHub commit metadata has no valid SHA")

        revision = revision.lower()
        return Resolution(
            type=source.type,
            version=source.options.ref,
            source_revision=revision,
            url=f"{api_root}/tarball/{revision}",
            target=source.target,
            media_type="application/gzip",
        )
