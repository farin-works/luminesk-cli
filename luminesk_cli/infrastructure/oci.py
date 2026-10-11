"""Resolve mutable Docker image references to immutable repository digests."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence

from luminesk_cli.domain.errors import ResolutionError, ValidationError
from luminesk_cli.domain.primitives import validate_pinned_image
from luminesk_cli.domain.progress import report_progress

IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class OciImageResolver:
    def __init__(self, runner: CommandRunner = subprocess.run) -> None:
        self._runner = runner

    def resolve(self, image: str, *, allow_pull: bool = True) -> str:
        if not IMAGE_RE.fullmatch(image):
            raise ResolutionError("runtime image reference is invalid", image=image)

        if "@" in image:
            try:
                return validate_pinned_image(image, "runtime.image")
            except ValidationError as exc:
                raise ResolutionError(
                    "runtime image digest is invalid", image=image
                ) from exc

        digests = self._inspect(image)

        if not digests and allow_pull:
            report_progress("image")
            self._run(["docker", "pull", image])
            digests = self._inspect(image)

        if not digests:
            raise ResolutionError("runtime image has no repository digest", image=image)

        repository = image.split("@", 1)[0]

        if "/" not in repository.split(":", 1)[0] and repository.count(":") <= 1:
            repository_name = repository.rsplit(":", 1)[0]
        else:
            last_slash = repository.rfind("/")
            last_colon = repository.rfind(":")
            repository_name = (
                repository[:last_colon] if last_colon > last_slash else repository
            )

        matching = [
            digest
            for digest in digests
            if digest.startswith(f"{repository_name}@sha256:")
        ]
        return matching[0] if matching else digests[0]

    def _inspect(self, image: str) -> list[str]:
        result = self._run(
            [
                "docker",
                "image",
                "inspect",
                image,
                "--format",
                "{{json .RepoDigests}}",
            ],
            check=False,
        )

        if result.returncode != 0:
            return []

        try:
            value = json.loads(result.stdout.strip())
        except json.JSONDecodeError as exc:
            raise ResolutionError("Docker returned invalid image metadata") from exc

        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ResolutionError("Docker returned invalid repository digests")

        try:
            return [
                validate_pinned_image(item, "docker.image.repoDigest") for item in value
            ]
        except ValidationError as exc:
            raise ResolutionError("Docker returned invalid repository digests") from exc

    def _run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                argv,
                check=check,
                capture_output=True,
                text=True,
                shell=False,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ResolutionError(f"Docker image resolution failed: {exc}") from exc
