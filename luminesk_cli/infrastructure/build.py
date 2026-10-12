"""Declarative package assembly and optional isolated Dockerfile builds."""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from luminesk_cli.domain.errors import SecurityError, TransactionError, ValidationError
from luminesk_cli.domain.inputs import resolve_inputs
from luminesk_cli.domain.lockfile import Lockfile
from luminesk_cli.domain.manifest import (
    Build,
    Check,
    FileSpec,
    GitHubSourceOptions,
    Manifest,
    SourceSpec,
)
from luminesk_cli.domain.package import PackageFile, PackageMetadata, ServerPackage
from luminesk_cli.domain.progress import report_progress
from luminesk_cli.infrastructure.cache import ContentCache, digest_file
from luminesk_cli.infrastructure.docker_process import docker_failure, run_docker
from luminesk_cli.infrastructure.dockerfile import rewrite_dockerfile
from luminesk_cli.infrastructure.oci import OciImageResolver
from luminesk_cli.infrastructure.package import (
    MAX_PACKAGE_FILES,
    MAX_PACKAGE_SIZE,
    write_package,
)
from luminesk_cli.infrastructure.security.archive import ArchiveLimits, extract_archive
from luminesk_cli.infrastructure.template import (
    apply_ownership_overrides,
    materialize_template,
    read_template_tree,
    references_secret_input,
)

MAX_BUILD_CONTEXT_FILES = 20_000
MAX_BUILD_CONTEXT_SIZE = 1024 * 1024 * 1024


class DockerfileBuilder:
    """Run a declared Dockerfile without host mounts, socket, or privileges."""

    def build(
        self,
        recipe_root: Path,
        spec: Build,
        destination: Path,
        images: dict[str, str],
        *,
        target: str,
    ) -> None:
        OciImageResolver().require_target(target)
        with tempfile.TemporaryDirectory(
            prefix="luminesk-build-context-"
        ) as context_name:
            context = Path(context_name)
            _copy_build_context(recipe_root, context)
            dockerfile = context / spec.file

            if not dockerfile.is_file() or dockerfile.is_symlink():
                raise ValidationError(
                    "declared Dockerfile is not a regular context file",
                    path=spec.file,
                )

            pinned_dockerfile = context / ".luminesk-pinned.Dockerfile"
            pinned_dockerfile.write_text(
                rewrite_dockerfile(
                    dockerfile.read_text(encoding="utf-8"),
                    images,
                ),
                encoding="utf-8",
                newline="\n",
            )
            destination.mkdir(parents=True, exist_ok=True)
            network = "default" if spec.network else "none"
            build_id = uuid.uuid4().hex
            image_tag = f"luminesk-build:{build_id}"
            container_name = f"luminesk-build-output-{build_id}"
            argv = [
                "docker",
                "build",
                "--platform",
                target,
                "--file",
                str(pinned_dockerfile),
                "--network",
                network,
                "--memory",
                spec.memory,
                "--cpu-quota",
                str(spec.cpu * 100_000),
                "--tag",
                image_tag,
                str(context),
            ]

            try:
                report_progress("build")
                result = run_docker(
                    argv,
                    timeout=spec.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise TransactionError(
                    "Dockerfile build timed out; check build steps, Docker Engine and network",
                    timeout=spec.timeout,
                ) from exc
            except OSError as exc:
                raise TransactionError("cannot execute Dockerfile build") from exc

            if result.returncode != 0:
                raise TransactionError(
                    docker_failure("build", result.stderr or result.stdout),
                    exit_code=result.returncode,
                )

            try:
                create_result = subprocess.run(
                    [
                        "docker",
                        "create",
                        "--pull=never",
                        "--platform",
                        target,
                        "--name",
                        container_name,
                        image_tag,
                        "true",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    shell=False,
                )

                if create_result.returncode != 0:
                    raise TransactionError(
                        "cannot create Dockerfile output container",
                        stderr=create_result.stderr[-4000:],
                    )

                copy_result = subprocess.run(
                    [
                        "docker",
                        "cp",
                        f"{container_name}:{spec.output}/.",
                        str(destination),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    shell=False,
                )

                if copy_result.returncode != 0:
                    raise TransactionError(
                        "cannot extract declared Dockerfile output",
                        output=spec.output,
                        stderr=copy_result.stderr[-4000:],
                    )
            finally:
                subprocess.run(
                    ["docker", "rm", "--force", container_name],
                    check=False,
                    capture_output=True,
                    shell=False,
                    timeout=30,
                )
                subprocess.run(
                    ["docker", "image", "rm", "--force", image_tag],
                    check=False,
                    capture_output=True,
                    shell=False,
                    timeout=30,
                )


class DeclarativeBuilder:
    def __init__(
        self,
        cache: ContentCache,
        *,
        dockerfile_builder: DockerfileBuilder | None = None,
        archive_limits: ArchiveLimits = ArchiveLimits(),
    ) -> None:
        self.cache = cache
        self.dockerfile_builder = dockerfile_builder or DockerfileBuilder()
        self.archive_limits = archive_limits

    def build(
        self,
        manifest: Manifest,
        lockfile: Lockfile,
        recipe_root: Path,
        output: Path,
        *,
        inputs: Mapping[str, str | int | bool] | None = None,
    ) -> ServerPackage:
        report_progress("build")
        if manifest.digest != lockfile.manifest_digest:
            raise ValidationError("lockfile does not match manifest")

        values = resolve_inputs(manifest, inputs or {})
        template_tree = read_template_tree(recipe_root, manifest)

        with tempfile.TemporaryDirectory(
            prefix="luminesk-package-stage-"
        ) as stage_name:
            payload = Path(stage_name) / "payload"
            payload.mkdir()
            ownership: dict[str, str] = {}
            mode_overrides: dict[str, int] = {}

            if manifest.build is not None:
                if lockfile.build is None:
                    raise ValidationError("lockfile has no pinned Dockerfile images")

                self.dockerfile_builder.build(
                    recipe_root,
                    manifest.build,
                    payload,
                    lockfile.build.images,
                    target=lockfile.target,
                )
                _record_tree(payload, payload, ownership, "managed")

            for source in manifest.sources:
                resolved = lockfile.sources.get(source.id)

                if resolved is None:
                    if source.platforms and lockfile.target not in source.platforms:
                        continue

                    raise ValidationError(f"lockfile is missing source {source.id}")

                blob = self.cache.restore(resolved.digest)

                if blob is None:
                    raise ValidationError(
                        f"source {source.id} is absent from content cache"
                    )

                target = payload / source.target

                if source.extract:
                    if target.exists() and not target.is_dir():
                        raise ValidationError(
                            f"source target conflicts with existing file: {source.target}"
                        )

                    before = set(ownership)
                    _extract_source(
                        source,
                        blob.path,
                        target,
                        self.archive_limits,
                    )
                    _record_tree(payload, target, ownership, "managed", previous=before)
                else:
                    _ensure_new_target(target, source.target)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(blob.path, target)
                    ownership[source.target] = "managed"

            if template_tree is not None:
                materialize_template(
                    template_tree,
                    payload,
                    manifest,
                    values,
                    ownership,
                    mode_overrides,
                )

            for file_spec in manifest.files:
                _apply_recipe_file(
                    recipe_root,
                    payload,
                    manifest,
                    file_spec,
                    values,
                    ownership,
                    mode_overrides,
                )

            apply_ownership_overrides(payload, manifest, ownership)

            _run_file_checks(payload, manifest.checks, phase="post-build")
            package_files = _package_files(
                payload,
                ownership,
                manifest,
                mode_overrides,
            )
            metadata = PackageMetadata(
                name=manifest.package.name,
                version=manifest.package.version,
                manifest_digest=manifest.digest,
                lock_digest=lockfile.digest,
                target=lockfile.target,
                recipe_revision=(
                    lockfile.recipe.revision if lockfile.recipe is not None else None
                ),
                files=package_files,
            )
            return write_package(output, payload, metadata)


def _extract_source(
    source: SourceSpec,
    archive: Path,
    target: Path,
    limits: ArchiveLimits,
) -> None:
    if not isinstance(source.options, GitHubSourceOptions):
        target.mkdir(parents=True, exist_ok=True)
        extract_archive(archive, target, limits=limits)
        return

    with tempfile.TemporaryDirectory(prefix="luminesk-github-source-") as temporary:
        extracted = Path(temporary)
        extract_archive(archive, extracted, limits=limits)
        roots = list(extracted.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise SecurityError("GitHub source archive has an invalid root layout")

        selected = roots[0]
        if source.options.path is not None:
            selected = selected / source.options.path
        if not selected.exists():
            raise ValidationError(
                "GitHub source path does not exist in the resolved commit",
                path=source.options.path,
            )
        if selected.is_symlink():
            raise SecurityError("GitHub source path may not be a symlink")

        if selected.is_file():
            _ensure_new_target(target, source.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(selected, target)
            return
        if not selected.is_dir():
            raise SecurityError("GitHub source path is not a regular file or directory")

        target.mkdir(parents=True, exist_ok=True)
        for item in selected.iterdir():
            destination = target / item.name
            if destination.exists():
                raise ValidationError(
                    f"source target conflicts with existing path: {source.target}/{item.name}"
                )
            if item.is_dir():
                shutil.copytree(item, destination)
            elif item.is_file():
                shutil.copyfile(item, destination)
            else:
                raise SecurityError("GitHub source contains a special file")


def _render_template(content: bytes, values: Mapping[str, str | int | bool]) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("template files must be UTF-8") from exc

    pattern = re.compile(r"\$\{input\.([A-Za-z0-9_-]+)\}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)

        if name not in values:
            raise ValidationError(f"template references missing input: {name}")

        value = values[name]
        return str(value).lower() if isinstance(value, bool) else str(value)

    return pattern.sub(replace, text).encode("utf-8")


def _apply_recipe_file(
    recipe_root: Path,
    payload: Path,
    manifest: Manifest,
    spec: FileSpec,
    values: Mapping[str, str | int | bool],
    ownership: dict[str, str],
    mode_overrides: dict[str, int],
) -> None:
    target = payload / spec.target

    if spec.mode == "data" and not (recipe_root / spec.source).exists():
        _ensure_new_target(target, spec.target)
        target.mkdir(parents=True)
        ownership[spec.target] = spec.mode
        return

    source = (recipe_root / spec.source).resolve()

    if not source.is_relative_to(recipe_root.resolve()) or source.is_symlink():
        raise SecurityError("recipe file escapes recipe root", path=spec.source)

    if not source.exists():
        raise ValidationError("recipe file does not exist", path=spec.source)

    _ensure_new_target(target, spec.target)

    if source.is_dir():
        target.mkdir(parents=True)
        ownership[spec.target] = spec.mode

        for item in source.rglob("*"):
            if item.is_symlink():
                raise SecurityError("recipe symlinks are forbidden", path=str(item))

            relative = item.relative_to(source)
            destination = target / relative

            if item.is_dir():
                destination.mkdir()
                ownership[destination.relative_to(payload).as_posix()] = spec.mode
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            content = item.read_bytes()
            contains_secret = spec.template and references_secret_input(
                content, manifest
            )

            if spec.template:
                content = _render_template(content, values)

            destination.touch(mode=0o600 if contains_secret else 0o666)
            destination.write_bytes(content)
            destination_relative = destination.relative_to(payload).as_posix()
            if contains_secret:
                destination.chmod(0o600)
                mode_overrides[destination_relative] = 0o600
            ownership[destination_relative] = spec.mode
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        contains_secret = spec.template and references_secret_input(content, manifest)

        if spec.template:
            content = _render_template(content, values)

        target.touch(mode=0o600 if contains_secret else 0o666)
        target.write_bytes(content)
        if contains_secret:
            intended_mode = 0o700 if spec.executable else 0o600
            target.chmod(intended_mode)
            mode_overrides[spec.target] = intended_mode
        ownership[spec.target] = spec.mode
    else:
        raise SecurityError("recipe source is not a regular file or directory")

    if spec.executable:
        target.chmod(target.stat().st_mode | stat.S_IXUSR)


def _ensure_new_target(target: Path, relative: str) -> None:
    if target.exists() or target.is_symlink():
        raise ValidationError(f"package target is declared more than once: {relative}")


def _record_tree(
    payload: Path,
    root: Path,
    ownership: dict[str, str],
    mode: str,
    *,
    previous: set[str] | None = None,
) -> None:
    previous = previous or set()

    for path in root.rglob("*"):
        if path.is_symlink():
            raise SecurityError("build output symlinks are forbidden", path=str(path))

        if not (path.is_file() or path.is_dir()):
            raise SecurityError(
                "build output special files are forbidden", path=str(path)
            )

        relative = path.relative_to(payload).as_posix()

        if relative in previous:
            raise ValidationError(f"build output collision: {relative}")

        ownership[relative] = mode


def _package_files(
    payload: Path,
    ownership: Mapping[str, str],
    manifest: Manifest,
    mode_overrides: Mapping[str, int],
) -> tuple[PackageFile, ...]:
    result: list[PackageFile] = []
    total_size = 0

    for path in sorted(payload.rglob("*")):
        if len(result) >= MAX_PACKAGE_FILES:
            raise SecurityError("package payload contains too many files")

        relative = path.relative_to(payload).as_posix()
        if relative in mode_overrides:
            mode = mode_overrides[relative]
        elif path.is_dir():
            mode = 0o755
        elif _is_declared_executable(relative, manifest):
            mode = 0o755
        else:
            mode = 0o644
        owner = ownership.get(relative, "managed")

        if owner not in {"managed", "preserve", "generated", "data"}:
            raise ValidationError(f"invalid ownership mode for {relative}")

        if path.is_dir():
            result.append(
                PackageFile(
                    path=relative,
                    type="directory",
                    mode=mode,
                    size=0,
                    digest=None,
                    ownership=owner,  # type: ignore[arg-type]
                )
            )
            continue

        digest, size = digest_file(path)
        total_size += size

        if total_size > MAX_PACKAGE_SIZE:
            raise SecurityError("package payload exceeds expanded size limit")

        result.append(
            PackageFile(
                path=relative,
                type="file",
                mode=mode,
                size=size,
                digest=digest,
                ownership=owner,  # type: ignore[arg-type]
            )
        )

    return tuple(result)


def _is_declared_executable(path: str, manifest: Manifest) -> bool:
    if any(
        path == policy or path.startswith(f"{policy}/")
        for policy in manifest.ownership.executable
    ):
        return True

    return any(spec.executable and spec.target == path for spec in manifest.files)


def _run_file_checks(payload: Path, checks: Sequence[Check], *, phase: str) -> None:
    for check in checks:
        if check.phase != phase or check.kind != "file" or check.path is None:
            continue

        exists = (payload / check.path).is_file()

        if check.required and not exists:
            raise ValidationError(
                f"required {phase} file check failed: {check.id}", path=check.path
            )


def _copy_build_context(source: Path, destination: Path) -> None:
    count = 0
    size = 0
    excluded_names = {".git", ".luminesk_cli", "worlds", "logs"}

    for path in source.rglob("*"):
        relative = path.relative_to(source)

        if any(part in excluded_names for part in relative.parts):
            continue

        if path.is_symlink():
            raise SecurityError(
                "build context symlinks are forbidden", path=str(relative)
            )

        target = destination / relative
        count += 1

        if count > MAX_BUILD_CONTEXT_FILES:
            raise SecurityError("build context contains too many files")

        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        if not path.is_file():
            raise SecurityError(
                "build context contains a special file", path=str(relative)
            )

        size += path.stat().st_size

        if size > MAX_BUILD_CONTEXT_SIZE:
            raise SecurityError("build context exceeds size limit")

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
