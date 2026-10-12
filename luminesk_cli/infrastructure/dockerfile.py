"""Parse and pin Dockerfile base images without executing the recipe."""

from __future__ import annotations

import re
from pathlib import Path

from luminesk_cli.domain.errors import SecurityError, ValidationError
from luminesk_cli.domain.manifest import Build
from luminesk_cli.infrastructure.oci import OciImageResolver

FROM_RE = re.compile(
    r"^(?P<prefix>\s*FROM\s+(?:--platform=\S+\s+)?)"
    r"(?P<image>\S+)"
    r"(?P<suffix>\s+(?:AS\s+\S+)\s*|\s*)$",
    re.IGNORECASE,
)
SYNTAX_RE = re.compile(r"^\s*#\s*syntax\s*=\s*(?P<image>\S+)\s*$", re.IGNORECASE)
PINNED_SYNTAX_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def dockerfile_base_images(path: Path) -> tuple[str, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"cannot read Dockerfile: {exc}") from exc

    images = []
    aliases = set()

    _validate_syntax_frontend(lines)

    for line in lines:
        match = FROM_RE.fullmatch(line)

        if match is None:
            if line.lstrip().upper().startswith("FROM "):
                raise ValidationError(f"unsupported Dockerfile FROM syntax: {line}")

            continue

        image = match.group("image")

        if "$" in image:
            raise SecurityError("dynamic Dockerfile FROM images are forbidden")

        if image.lower() != "scratch" and image.lower() not in aliases:
            images.append(image)

        suffix = match.group("suffix").strip().split()

        if len(suffix) == 2 and suffix[0].lower() == "as":
            aliases.add(suffix[1].lower())

    if not images:
        raise ValidationError("Dockerfile declares no external base image")

    return tuple(dict.fromkeys(images))


def resolve_build_images(
    recipe_root: Path,
    build: Build,
    resolver: OciImageResolver,
    *,
    target: str | None = None,
) -> dict[str, str]:
    dockerfile = (recipe_root / build.file).resolve()

    if not dockerfile.is_relative_to(recipe_root.resolve()):
        raise SecurityError("Dockerfile escapes recipe root")

    return {
        image: resolver.resolve(image, target=target)
        for image in dockerfile_base_images(dockerfile)
    }


def rewrite_dockerfile(content: str, images: dict[str, str]) -> str:
    source_lines = content.splitlines()
    _validate_syntax_frontend(source_lines)
    lines = []
    used = set()

    for line in content.splitlines():
        match = FROM_RE.fullmatch(line)

        if match is not None and match.group("image") in images:
            used.add(match.group("image"))
            line = (
                f"{match.group('prefix')}{images[match.group('image')]}"
                f"{match.group('suffix')}"
            )

        lines.append(line)

    missing = set(images) - used

    if missing:
        raise ValidationError(
            f"pinned Dockerfile image is no longer present: {sorted(missing)[0]}"
        )

    return "\n".join(lines) + "\n"


def _validate_syntax_frontend(lines: list[str]) -> None:
    directives = []
    for line in lines:
        match = SYNTAX_RE.fullmatch(line)
        if match is not None:
            directives.append(match.group("image"))
        elif line.lstrip().lower().startswith("# syntax="):
            raise ValidationError("unsupported Dockerfile syntax directive")

    if len(directives) > 1:
        raise ValidationError("Dockerfile declares multiple syntax frontends")
    if directives and PINNED_SYNTAX_RE.fullmatch(directives[0]) is None:
        raise SecurityError(
            "Dockerfile syntax frontend must be pinned by sha256 digest"
        )
