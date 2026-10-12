"""Bounded streaming downloader shared by every remote source adapter."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from luminesk_cli.domain.errors import NetworkError, SecurityError
from luminesk_cli.domain.progress import report_progress
from luminesk_cli.infrastructure.cache import CachedBlob, ContentCache
from luminesk_cli.infrastructure.security.network import (
    AddressResolver,
    _resolve_addresses,
    validate_redirect,
    validate_remote_url,
)
from luminesk_cli.infrastructure.security.transport import (
    ALLOW_PRIVATE_NETWORK_EXTENSION,
    create_secure_client,
)

DOWNLOAD_CHUNK_SIZE = 256 * 1024
MAX_REDIRECTS = 5
SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "private-token", "proxy-authorization", "job-token"}
)


class SecureFetcher:
    def __init__(
        self,
        cache: ContentCache,
        *,
        client: httpx.Client | None = None,
        address_resolver: AddressResolver = _resolve_addresses,
    ) -> None:
        self.cache = cache
        self._client = client
        self._address_resolver = address_resolver

    def fetch(
        self,
        url: str,
        *,
        max_size: int,
        expected_digest: str | None = None,
        expected_size: int | None = None,
        allow_http: bool = False,
        allow_private_network: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> CachedBlob:
        if expected_digest is not None:
            report_progress("verify")
            cached = self.cache.restore(expected_digest)

            if cached is not None:
                return cached

        report_progress("download")
        owned_client = self._client is None
        client = self._client or create_secure_client(resolver=self._address_resolver)

        try:
            return self._fetch_with_client(
                client,
                url,
                max_size=max_size,
                expected_digest=expected_digest,
                expected_size=expected_size,
                allow_http=allow_http,
                allow_private_network=allow_private_network,
                headers=headers,
            )
        finally:
            if owned_client:
                client.close()

    def _fetch_with_client(
        self,
        client: httpx.Client,
        url: str,
        *,
        max_size: int,
        expected_digest: str | None,
        expected_size: int | None,
        allow_http: bool,
        allow_private_network: bool,
        headers: Mapping[str, str] | None,
    ) -> CachedBlob:
        current_url = validate_remote_url(
            url,
            allow_http=allow_http,
            allow_private_network=allow_private_network,
            resolver=self._address_resolver,
        )
        credential_host = urlsplit(current_url).hostname

        for redirect_count in range(MAX_REDIRECTS + 1):
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
                    follow_redirects=False,
                    headers=request_headers,
                    extensions={ALLOW_PRIVATE_NETWORK_EXTENSION: allow_private_network},
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")

                        if not location:
                            raise NetworkError(
                                "redirect response has no Location header",
                                url=current_url,
                            )

                        if redirect_count == MAX_REDIRECTS:
                            raise NetworkError("too many redirects", url=current_url)

                        current_url = validate_redirect(
                            current_url,
                            location,
                            allow_http=allow_http,
                            allow_private_network=allow_private_network,
                            resolver=self._address_resolver,
                        )
                        continue

                    response.raise_for_status()
                    return self._consume(
                        response,
                        max_size=max_size,
                        expected_digest=expected_digest,
                        expected_size=expected_size,
                    )
            except httpx.HTTPStatusError as exc:
                raise NetworkError(
                    f"download failed with HTTP {exc.response.status_code}",
                    url=current_url,
                    status=exc.response.status_code,
                ) from exc
            except httpx.RequestError as exc:
                raise NetworkError(f"download failed: {exc}", url=current_url) from exc

        raise NetworkError("too many redirects", url=current_url)

    def _consume(
        self,
        response: httpx.Response,
        *,
        max_size: int,
        expected_digest: str | None,
        expected_size: int | None,
    ) -> CachedBlob:
        declared_size = _content_length(response)

        if declared_size is not None and declared_size > max_size:
            raise SecurityError(
                "download exceeds configured size limit",
                size=declared_size,
                limit=max_size,
            )

        if expected_size is not None and declared_size not in {None, expected_size}:
            raise SecurityError(
                "download Content-Length does not match lockfile",
                expected=expected_size,
                actual=declared_size,
            )

        self.cache.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.cache.root,
            prefix="download-",
            suffix=".part",
        )
        temporary = Path(temporary_name)
        hasher = hashlib.sha256()
        size = 0
        total = expected_size if expected_size is not None else declared_size

        try:
            with os.fdopen(descriptor, "wb") as handle:
                report_progress("download", completed=0, total=total)
                for chunk in response.iter_bytes(DOWNLOAD_CHUNK_SIZE):
                    size += len(chunk)

                    if size > max_size:
                        raise SecurityError(
                            "download exceeds configured size limit",
                            size=size,
                            limit=max_size,
                        )

                    handle.write(chunk)
                    hasher.update(chunk)
                    report_progress("download", completed=size, total=total)

                handle.flush()
                os.fsync(handle.fileno())

            report_progress("verify")
            digest = f"sha256:{hasher.hexdigest()}"

            if expected_size is not None and size != expected_size:
                raise SecurityError(
                    "download size does not match lockfile",
                    expected=expected_size,
                    actual=size,
                )

            if expected_digest is not None and digest != expected_digest:
                raise SecurityError(
                    "download digest does not match lockfile",
                    expected=expected_digest,
                    actual=digest,
                )

            return self.cache.store(temporary, digest)
        finally:
            temporary.unlink(missing_ok=True)


def _content_length(response: httpx.Response) -> int | None:
    # ``httpx.Response.iter_bytes()`` transparently decodes content encodings.
    # In that case Content-Length describes the encoded wire representation,
    # while the lockfile and provider metadata describe the decoded artifact.
    # The decoded body is still bounded and verified below while streaming.
    content_encoding = response.headers.get("content-encoding", "identity")

    if content_encoding.strip().casefold() not in {"", "identity"}:
        return None

    raw_value = response.headers.get("content-length")

    if raw_value is None:
        return None

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SecurityError("invalid Content-Length header") from exc

    if value < 0:
        raise SecurityError("invalid negative Content-Length header")

    return value
