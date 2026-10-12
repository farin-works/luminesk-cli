"""Resolve mutable Docker image references to immutable repository digests."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Sequence

from luminesk_cli.domain.errors import ResolutionError, ValidationError
from luminesk_cli.domain.primitives import validate_pinned_image
from luminesk_cli.domain.progress import report_progress
from luminesk_cli.infrastructure.docker_process import docker_failure, run_docker
from luminesk_cli.infrastructure.platform import ARCH_NAMES

IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ENGINE_TIMEOUT = 30
PULL_TIMEOUT = 1800
LOGGER = logging.getLogger(__name__)


class OciImageResolver:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner

    def target_platform(self) -> str:
        report_progress("engine")
        result = self._run(
            ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"]
        )
        return _platform(result.stdout.strip())

    def require_target(self, target: str) -> None:
        actual = self.target_platform()
        if target != actual:
            raise ResolutionError(
                f"locked target {target} does not match Docker Engine {actual}; "
                "select the matching Docker context; the lock was not changed",
                target=target,
            )

    def ensure_available(self, image: str, target: str) -> None:
        """Pull a missing pin explicitly and verify the selected image platform."""
        validate_pinned_image(image, "runtime.image")
        self.require_target(target)
        actual = self._image_platform(image)
        if actual != target:
            self._pull(image, target)
            actual = self._image_platform(image)
        if actual != target:
            raise ResolutionError(
                f"Docker image platform {actual or 'missing'} does not match {target}"
            )

    def resolve(
        self, image: str, *, allow_pull: bool = True, target: str | None = None
    ) -> str:
        if not IMAGE_RE.fullmatch(image):
            raise ResolutionError("runtime image reference is invalid", image=image)

        if "@" in image:
            try:
                return validate_pinned_image(image, "runtime.image")
            except ValidationError as exc:
                raise ResolutionError(
                    "runtime image digest is invalid", image=image
                ) from exc

        target = target or self.target_platform()
        actual = self._image_platform(image)
        digests = self._inspect(image) if actual == target else []

        if not digests and allow_pull:
            self._pull(image, target)
            if self._image_platform(image) != target:
                raise ResolutionError("Docker pulled an image for a different platform")
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

    def _pull(self, image: str, target: str) -> None:
        report_progress("image")
        self._run(
            ["docker", "pull", "--platform", target, image],
            timeout=PULL_TIMEOUT,
            on_line=_PullProgress(),
        )

    def _image_platform(self, image: str) -> str | None:
        report_progress("image-inspect")
        result = self._run(
            [
                "docker",
                "image",
                "inspect",
                image,
                "--format",
                "{{.Os}}/{{.Architecture}}",
            ],
            check=False,
        )
        if result.returncode:
            if _missing_image(result):
                return None
            raise ResolutionError(
                docker_failure("image inspect", result.stderr or result.stdout)
            )
        return _platform(result.stdout.strip())

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
            if _missing_image(result):
                return []
            raise ResolutionError(
                docker_failure("image inspect", result.stderr or result.stdout)
            )

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
        timeout: float = ENGINE_TIMEOUT,
        on_line: Callable[[str], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        LOGGER.debug(
            "Docker image command started operation=%s timeout=%s", argv[1], timeout
        )
        try:
            if self._runner is None:
                result = run_docker(argv, timeout=timeout, on_line=on_line)
            else:
                result = self._runner(
                    argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=timeout,
                )
                if on_line is not None:
                    for line in (result.stdout + result.stderr).splitlines():
                        on_line(line)
        except subprocess.TimeoutExpired as exc:
            raise ResolutionError(
                f"Docker {argv[1]} timed out after {timeout:g}s; "
                "check Docker Desktop/WSL2, DNS, proxy and registry connectivity",
                operation=argv[1],
                timeout=timeout,
            ) from exc
        except OSError as exc:
            raise ResolutionError(
                "cannot execute Docker; install/start Docker Desktop and check the active context"
            ) from exc
        if check and result.returncode:
            raise ResolutionError(
                docker_failure(argv[1], result.stderr or result.stdout),
                exit_code=result.returncode,
            )
        LOGGER.debug(
            "Docker image command completed operation=%s exit_code=%d",
            argv[1],
            result.returncode,
        )
        return result


def _platform(value: str) -> str:
    os_name, _, architecture = value.lower().partition("/")
    architecture = ARCH_NAMES.get(architecture, "")
    if os_name != "linux" or not architecture:
        raise ResolutionError(
            "Docker must provide Linux containers on amd64 or arm64; "
            "switch Docker Desktop to Linux containers and check the active context"
        )
    return f"{os_name}/{architecture}"


def _missing_image(result: subprocess.CompletedProcess[str]) -> bool:
    text = (result.stderr or result.stdout).lower()
    return "no such image" in text or "no such object" in text


class _PullProgress:
    """Forward only recognized engine states/counts, never raw registry output."""

    def __init__(self) -> None:
        self.completed: set[str] = set()

    def __call__(self, line: str) -> None:
        match = re.fullmatch(
            r"([0-9a-f]{12,64}): (Pull complete|Already exists)", line.strip()
        )
        if match and len(self.completed) < 4096:
            self.completed.add(match[1])
            report_progress("image", completed=len(self.completed), unit="layers")
        else:
            state = re.match(r"^[0-9a-f]{12,64}: (Downloading|Extracting)\b", line)
            if state is None:
                return
            stage = "image-download" if state[1] == "Downloading" else "image-extract"
            sizes = re.search(
                r"([0-9]{1,12}(?:\.[0-9]{1,6})?)(B|kB|MB|GB)\s*/\s*"
                r"([0-9]{1,12}(?:\.[0-9]{1,6})?)(B|kB|MB|GB)\s*$",
                line,
            )
            if sizes is None:
                report_progress(stage)
            else:
                factors = {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3}
                report_progress(
                    stage,
                    completed=int(float(sizes[1]) * factors[sizes[2]]),
                    total=int(float(sizes[3]) * factors[sizes[4]]),
                )
