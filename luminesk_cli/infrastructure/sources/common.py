"""Bounded metadata requests and small version-selection primitives."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from luminesk_cli.domain.errors import NetworkError, ResolutionError
from luminesk_cli.domain.manifest import SourceSpec
from luminesk_cli.domain.progress import report_progress
from luminesk_cli.infrastructure.security.network import validate_remote_url
from luminesk_cli.infrastructure.security.transport import (
    ALLOW_PRIVATE_NETWORK_EXTENSION,
)

MAX_METADATA_SIZE = 2 * 1024 * 1024
MAX_METADATA_REDIRECTS = 5
SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "private-token", "proxy-authorization", "job-token"}
)
SEMVER_PARTS_RE = re.compile(
    r"^v?(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)


def request_metadata(
    client: httpx.Client,
    url: str,
    source: SourceSpec,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    report_progress("metadata")
    current_url = url
    credential_host = urlsplit(url).hostname

    for redirect_count in range(MAX_METADATA_REDIRECTS + 1):
        validate_remote_url(
            current_url,
            allow_http=source.allow_http,
            allow_private_network=source.allow_private_network,
        )

        request_headers = {
            key: value
            for key, value in (headers or {}).items()
            if urlsplit(current_url).hostname == credential_host
            or key.lower() not in SENSITIVE_HEADERS
        }

        try:
            with client.stream(
                "GET",
                current_url,
                headers=request_headers,
                follow_redirects=False,
                extensions={
                    ALLOW_PRIVATE_NETWORK_EXTENSION: source.allow_private_network
                },
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")

                    if not location:
                        raise NetworkError(
                            "metadata redirect has no Location header", url=current_url
                        )

                    if redirect_count == MAX_METADATA_REDIRECTS:
                        raise NetworkError(
                            "too many metadata redirects", url=current_url
                        )

                    current_url = urljoin(current_url, location)
                    continue

                response.raise_for_status()
                declared_size = _metadata_content_length(response)

                if declared_size is not None and declared_size > MAX_METADATA_SIZE:
                    raise ResolutionError(
                        "metadata response is too large", size=declared_size
                    )

                body = bytearray()

                for chunk in response.iter_bytes(64 * 1024):
                    body.extend(chunk)

                    if len(body) > MAX_METADATA_SIZE:
                        raise ResolutionError(
                            "metadata response is too large", size=len(body)
                        )

                decoded_headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower()
                    not in {"content-encoding", "content-length", "transfer-encoding"}
                }
                report_progress("resolve")
                return httpx.Response(
                    response.status_code,
                    headers=decoded_headers,
                    content=bytes(body),
                    request=response.request,
                )
        except httpx.HTTPStatusError as exc:
            raise NetworkError(
                f"metadata request failed with HTTP {exc.response.status_code}",
                url=current_url,
                status=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise NetworkError(
                f"metadata request failed: {exc}", url=current_url
            ) from exc

    raise NetworkError("too many metadata redirects", url=current_url)


def _metadata_content_length(response: httpx.Response) -> int | None:
    raw_value = response.headers.get("content-length")

    if raw_value is None:
        return None

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ResolutionError("invalid metadata Content-Length") from exc

    if value < 0:
        raise ResolutionError("invalid metadata Content-Length")

    return value


def request_json_object(
    client: httpx.Client,
    url: str,
    source: SourceSpec,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = request_metadata(client, url, source, headers=headers)

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ResolutionError("metadata is not valid JSON", url=url) from exc

    if not isinstance(payload, dict):
        raise ResolutionError("metadata JSON root must be an object", url=url)

    return payload


def parse_semver(value: str) -> tuple[int, int, int, str | None] | None:
    match = SEMVER_PARTS_RE.fullmatch(value.strip())

    if match is None:
        return None

    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        match.group(4),
    )


def version_matches(version: str, constraint: str | None, channel: str) -> bool:
    parsed = parse_semver(version)

    if channel == "stable" and parsed is not None and parsed[3] is not None:
        return False

    if constraint is None or constraint in {"", "latest", "*"}:
        return parsed is not None

    if not any(symbol in constraint for symbol in "<>=,"):
        return version.lstrip("v") == constraint.lstrip("v")

    if parsed is None:
        return False

    comparable = parsed[:3]

    for raw_clause in constraint.split(","):
        clause = raw_clause.strip()
        match = re.fullmatch(r"(<=|>=|<|>|==|=)?\s*(v?[0-9]+\.[0-9]+\.[0-9]+)", clause)

        if match is None:
            raise ResolutionError(f"unsupported version constraint: {clause}")

        operator = match.group(1) or "=="
        expected = parse_semver(match.group(2))
        assert expected is not None
        expected_parts = expected[:3]
        matches = {
            "<": comparable < expected_parts,
            "<=": comparable <= expected_parts,
            ">": comparable > expected_parts,
            ">=": comparable >= expected_parts,
            "=": comparable == expected_parts,
            "==": comparable == expected_parts,
        }[operator]

        if not matches:
            return False

    return True


def select_highest_version(
    versions: list[str], constraint: str | None, channel: str
) -> str:
    candidates = [
        (parsed, version)
        for version in versions
        if version_matches(version, constraint, channel)
        and (parsed := parse_semver(version)) is not None
    ]

    if not candidates:
        raise ResolutionError(
            f"no version matches {constraint or 'latest'} on channel {channel}"
        )

    candidates.sort(
        key=lambda item: (
            item[0][:3],
            item[0][3] is None,
            item[0][3] or "",
        ),
        reverse=True,
    )
    return candidates[0][1]
