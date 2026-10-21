"""GitLab CI artifact resolver pinned to a concrete successful job."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

from luminesk_cli.domain.errors import ResolutionError
from luminesk_cli.domain.manifest import GitLabJobArtifactOptions, SourceSpec
from luminesk_cli.infrastructure.sources.base import Resolution
from luminesk_cli.infrastructure.sources.common import request_metadata


class GitLabJobArtifactResolver:
    def resolve(self, source: SourceSpec, client: httpx.Client) -> Resolution:
        if not isinstance(source.options, GitLabJobArtifactOptions):
            raise ResolutionError("gitlab-job-artifact source has invalid options")

        options = source.options
        headers = {
            "User-Agent": "luminesk/2.0 (https://github.com/farin-works/luminesk-cli)"
        }
        token = os.environ.get("GITLAB_TOKEN")
        if token:
            headers["PRIVATE-TOKEN"] = token

        project = quote(options.project, safe="")
        api_root = f"{options.base_url.rstrip('/')}/api/v4/projects/{project}"
        response = request_metadata(
            client,
            f"{api_root}/jobs?scope[]=success&per_page=100",
            source,
            headers=headers,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ResolutionError("GitLab jobs metadata is not valid JSON") from exc
        if not isinstance(payload, list):
            raise ResolutionError("GitLab jobs metadata must be an array")

        job = _select_job(payload, options.ref, options.job)
        job_id = job.get("id")
        if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 1:
            raise ResolutionError("GitLab job has no valid id")
        commit = job.get("commit")
        revision = commit.get("id") if isinstance(commit, dict) else None
        if not isinstance(revision, str) or not revision:
            revision = f"job-{job_id}"
        artifact = quote(options.artifact, safe="/")

        return Resolution(
            type=source.type,
            version=str(job_id),
            source_revision=revision,
            url=f"{api_root}/jobs/{job_id}/artifacts/{artifact}",
            target=source.target,
        )


def _select_job(jobs: list[Any], ref: str, name: str) -> dict[str, Any]:
    matches = [
        job
        for job in jobs
        if isinstance(job, dict)
        and job.get("name") == name
        and job.get("ref") == ref
        and job.get("status") == "success"
    ]
    if not matches:
        raise ResolutionError(f"no successful GitLab job {name} exists for ref {ref}")
    matches.sort(key=_job_id, reverse=True)
    return matches[0]


def _job_id(item: dict[str, Any]) -> int:
    value = item.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1
