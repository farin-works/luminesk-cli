"""Resolve a manifest into an exact, reproducible lockfile."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from luminesk_cli.domain.errors import ResolutionError, SecurityError
from luminesk_cli.domain.lockfile import (
    BuildLock,
    Lockfile,
    RecipeLock,
    ResolvedSource,
    RuntimeLock,
)
from luminesk_cli.domain.manifest import LocalFileOptions, Manifest, SourceSpec
from luminesk_cli.domain.progress import report_progress
from luminesk_cli.domain.recipe import RecipeOrigin
from luminesk_cli.infrastructure.cache import ContentCache
from luminesk_cli.infrastructure.dockerfile import resolve_build_images
from luminesk_cli.infrastructure.fetch import SecureFetcher
from luminesk_cli.infrastructure.oci import OciImageResolver
from luminesk_cli.infrastructure.security.transport import create_secure_client
from luminesk_cli.infrastructure.sources.base import ResolverRegistry, default_registry

LOGGER = logging.getLogger(__name__)


class LockService:
    def __init__(
        self,
        cache: ContentCache,
        *,
        registry: ResolverRegistry | None = None,
        image_resolver: OciImageResolver | None = None,
        client: httpx.Client | None = None,
        fetcher: SecureFetcher | None = None,
    ) -> None:
        self.cache = cache
        self.registry = registry or default_registry()
        self.image_resolver = image_resolver or OciImageResolver()
        self.client = client
        self.fetcher = fetcher or SecureFetcher(cache, client=client)

    def create(
        self,
        manifest: Manifest,
        recipe_root: Path,
        *,
        recipe_origin: RecipeOrigin | None = None,
        target: str | None = None,
    ) -> Lockfile:
        target_platform = target or self.image_resolver.target_platform()
        LOGGER.debug(
            "lock resolution started target=%s sources=%d build=%s recipe_origin=%s",
            target_platform,
            len(manifest.sources),
            manifest.build is not None,
            recipe_origin is not None,
        )

        if recipe_origin is not None and (
            recipe_origin.manifest_digest != manifest.digest
            or recipe_origin.version != manifest.package.version
        ):
            raise ResolutionError("recipe origin does not match the resolved manifest")

        if (
            manifest.package.platforms
            and target_platform not in manifest.package.platforms
        ):
            raise ResolutionError(
                f"recipe does not support target {target_platform}",
                target=target_platform,
            )

        owned_client = self.client is None
        client = self.client or create_secure_client()

        try:
            sources = {
                source.id: self._resolve_source(source, recipe_root, client)
                for source in manifest.sources
                if not source.platforms or target_platform in source.platforms
            }
        finally:
            if owned_client:
                client.close()

        recipe = (
            RecipeLock(
                kind=recipe_origin.kind,
                source=recipe_origin.source,
                revision=recipe_origin.revision,
                ref=recipe_origin.ref,
                tracking=recipe_origin.tracking,
                entry=recipe_origin.entry,
                path=recipe_origin.path,
                version=recipe_origin.version,
                manifest_digest=recipe_origin.manifest_digest,
                template_digest=recipe_origin.template_digest,
            )
            if recipe_origin is not None
            else None
        )

        LOGGER.debug(
            "runtime image resolution started pinned=%s", "@" in manifest.runtime.image
        )
        runtime_image = self.image_resolver.resolve(
            manifest.runtime.image, target=target_platform
        )
        LOGGER.debug("runtime image resolution completed")
        lockfile = Lockfile(
            manifest_digest=manifest.digest,
            target=target_platform,
            sources=sources,
            runtime=RuntimeLock(image=runtime_image),
            build=(
                BuildLock(
                    images=resolve_build_images(
                        recipe_root,
                        manifest.build,
                        self.image_resolver,
                        target=target_platform,
                    )
                )
                if manifest.build is not None
                else None
            ),
            recipe=recipe,
        )
        LOGGER.debug(
            "lock resolution completed sources=%d build_images=%d",
            len(lockfile.sources),
            len(lockfile.build.images) if lockfile.build is not None else 0,
        )
        return lockfile

    def _resolve_source(
        self,
        source: SourceSpec,
        recipe_root: Path,
        client: httpx.Client,
    ) -> ResolvedSource:
        report_progress("resolve")
        LOGGER.debug(
            "source resolution started type=%s local=%s",
            source.type,
            source.type == "local-file",
        )
        if source.type == "local-file":
            resolved = self._resolve_local(source, recipe_root)
            LOGGER.debug("source resolution completed type=local-file")
            return resolved

        resolution = self.registry.resolve(source, client)
        LOGGER.debug("source provider metadata resolved type=%s", source.type)
        LOGGER.debug("source artifact fetch started type=%s", source.type)
        blob = self.fetcher.fetch(
            resolution.url,
            max_size=source.max_size,
            expected_digest=resolution.digest,
            expected_size=resolution.size,
            allow_http=source.allow_http,
            allow_private_network=source.allow_private_network,
        )
        LOGGER.debug(
            "source artifact verified type=%s size=%d",
            source.type,
            blob.size,
        )

        return ResolvedSource(
            type=source.type,
            version=resolution.version,
            source_revision=resolution.source_revision,
            url=resolution.url,
            size=blob.size,
            digest=blob.digest,
            target=source.target,
            media_type=resolution.media_type,
        )

    def _resolve_local(
        self,
        source: SourceSpec,
        recipe_root: Path,
    ) -> ResolvedSource:
        if not isinstance(source.options, LocalFileOptions):
            raise ResolutionError("local-file source has invalid options")

        root = recipe_root.resolve()
        candidate = (root / source.options.path).resolve()

        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise SecurityError(
                "local source must be a regular file inside the recipe root",
                path=str(candidate),
            )

        if candidate.stat().st_size > source.max_size:
            raise SecurityError(
                "local source exceeds configured size limit",
                size=candidate.stat().st_size,
                limit=source.max_size,
            )

        from luminesk_cli.infrastructure.cache import digest_file

        digest, _ = digest_file(candidate)
        blob = self.cache.store(candidate, digest)
        return ResolvedSource(
            type=source.type,
            version=source.options.version,
            source_revision=digest,
            url=f"local:{source.options.path}",
            size=blob.size,
            digest=blob.digest,
            target=source.target,
        )
