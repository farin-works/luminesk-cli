"""Deterministic ``.lumineskpkg`` writer and independent verifier."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from luminesk_cli.domain.errors import SecurityError, ValidationError
from luminesk_cli.domain.package import (
    PACKAGE_SUFFIX,
    PackageMetadata,
    ServerPackage,
    parse_package_metadata,
)
from luminesk_cli.domain.primitives import safe_relative_path
from luminesk_cli.domain.progress import report_progress
from luminesk_cli.infrastructure.cache import digest_file

METADATA_NAME = "metadata.json"
PAYLOAD_PREFIX = "payload/"
MAX_PACKAGE_FILES = 20_000
MAX_PACKAGE_SIZE = 4 * 1024 * 1024 * 1024
MAX_PACKAGE_METADATA_SIZE = 4 * 1024 * 1024
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def write_package(
    path: Path,
    payload_root: Path,
    metadata: PackageMetadata,
) -> ServerPackage:
    if path.suffix != PACKAGE_SUFFIX:
        raise ValidationError(f"package path must end with {PACKAGE_SUFFIX}")

    actual_paths = {
        item.relative_to(payload_root).as_posix()
        for item in payload_root.rglob("*")
        if item.is_file()
    }

    if any(item.is_symlink() for item in payload_root.rglob("*")):
        raise SecurityError("package payload symlinks are forbidden")

    expected_files = {item.path for item in metadata.files if item.type == "file"}

    if actual_paths != expected_files:
        raise ValidationError(
            "package metadata does not match staged payload",
            missing=sorted(expected_files - actual_paths),
            extra=sorted(actual_paths - expected_files),
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)

    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            _write_zip_bytes(archive, METADATA_NAME, metadata.to_bytes(), 0o644)

            for item in sorted(metadata.files, key=lambda entry: entry.path):
                archive_name = f"{PAYLOAD_PREFIX}{item.path}"

                if item.type == "directory":
                    _write_zip_bytes(archive, f"{archive_name}/", b"", item.mode)
                    continue

                source = payload_root / item.path
                _write_zip_file(archive, archive_name, source, item.mode)

        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

    report_progress("verify")
    return verify_package(path)


def _write_zip_bytes(
    archive: zipfile.ZipFile,
    name: str,
    content: bytes,
    mode: int,
) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(info, content)


def _write_zip_file(
    archive: zipfile.ZipFile,
    name: str,
    source: Path,
    mode: int,
) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16

    with source.open("rb") as input_file, archive.open(info, "w") as output:
        shutil.copyfileobj(input_file, output, length=256 * 1024)


def verify_package(path: Path) -> ServerPackage:
    package_digest, package_size = digest_file(path)

    if package_size > MAX_PACKAGE_SIZE:
        raise SecurityError("package exceeds size limit", size=package_size)

    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()

        if len(members) > MAX_PACKAGE_FILES + 1:
            raise SecurityError("package contains too many files")

        names = [member.filename for member in members]

        if not names or names.count(METADATA_NAME) != 1:
            raise ValidationError("package must contain exactly one metadata.json")

        if len(set(names)) != len(names):
            raise SecurityError("package contains duplicate members")

        for member in members:
            mode = member.external_attr >> 16
            kind = stat.S_IFMT(mode)

            if stat.S_ISLNK(mode):
                raise SecurityError(
                    "package symlinks are forbidden", path=member.filename
                )

            if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise SecurityError(
                    "package special files are forbidden", path=member.filename
                )

            if member.filename != METADATA_NAME:
                if not member.filename.startswith(PAYLOAD_PREFIX):
                    raise SecurityError(
                        "package member is outside payload", path=member.filename
                    )

                relative = member.filename.removeprefix(PAYLOAD_PREFIX).rstrip("/")
                safe_relative_path(relative, "package.member.path")

        metadata_member = archive.getinfo(METADATA_NAME)

        if metadata_member.file_size > MAX_PACKAGE_METADATA_SIZE:
            raise SecurityError("package metadata exceeds size limit")

        metadata = parse_package_metadata(archive.read(metadata_member))
        expected = {item.path: item for item in metadata.files}
        actual = {
            name.removeprefix(PAYLOAD_PREFIX).rstrip("/")
            for name in names
            if name.startswith(PAYLOAD_PREFIX)
        }

        if set(expected) != actual:
            raise ValidationError("package payload does not match metadata")

        expanded_size = 0

        for item_path, item in expected.items():
            member_name = f"{PAYLOAD_PREFIX}{item_path}"

            if item.type == "directory":
                member_name += "/"
                archive.getinfo(member_name)
                continue

            member = archive.getinfo(member_name)

            if member.file_size != item.size:
                raise SecurityError("package member size mismatch", path=item_path)

            import hashlib

            expanded_size += member.file_size

            if expanded_size > MAX_PACKAGE_SIZE:
                raise SecurityError("package expanded size exceeds limit")

            if (
                member.compress_size > 0
                and member.file_size > member.compress_size * 200
            ):
                raise SecurityError(
                    "package member exceeds compression-ratio limit", path=item_path
                )

            hasher = hashlib.sha256()

            with archive.open(member, "r") as handle:
                while chunk := handle.read(256 * 1024):
                    hasher.update(chunk)

            digest = f"sha256:{hasher.hexdigest()}"

            if digest != item.digest:
                raise SecurityError("package member digest mismatch", path=item_path)

    return ServerPackage(
        path=path,
        digest=package_digest,
        size=package_size,
        metadata=metadata,
    )


def extract_package(package: ServerPackage, destination: Path) -> None:
    """Extract a previously verified payload without trusting archive paths."""

    verified = verify_package(package.path)

    if verified.digest != package.digest:
        raise SecurityError("package changed after verification")

    if destination.exists() and any(destination.iterdir()):
        raise ValidationError("package staging destination must be empty")

    destination.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(package.path) as archive:
        for item in verified.metadata.files:
            target = destination / item.path

            if item.type == "directory":
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(item.mode)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            member = archive.getinfo(f"{PAYLOAD_PREFIX}{item.path}")

            with archive.open(member, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=256 * 1024)

            target.chmod(item.mode)
