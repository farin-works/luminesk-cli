"""Deterministic onedir ZIP with a bounded, shell-readable extraction inventory."""

from __future__ import annotations

import hashlib
import re
import stat
import sys
import zipfile
from pathlib import Path

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
MAX_BYTES = 2 * 1024**3
MAX_ENTRIES = 20000
SAFE_PATH = re.compile(r"[A-Za-z0-9_,+.-]+(?:/[A-Za-z0-9_,+.-]+)*")


def archive_bundle(source: Path, output: Path) -> None:
    source = source.resolve()
    if output.resolve().is_relative_to(source):
        raise ValueError("output must be outside the source bundle")
    if source.name != "nesk" or not source.is_dir():
        raise ValueError("source must be the nesk onedir directory")
    if not (source / "_internal").is_dir() or not any(
        (source / name).is_file() for name in ("nesk", "nesk.exe")
    ):
        raise ValueError("bundle requires nesk and _internal")
    paths = sorted(source.rglob("*"), key=lambda path: path.as_posix())
    if len(paths) > MAX_ENTRIES:
        raise ValueError("bundle has too many entries")
    inventory: list[str] = []
    entries: list[tuple[Path, zipfile.ZipInfo, bytes | None]] = []
    total = 0
    for path in paths:
        relative = path.relative_to(source).as_posix()
        if (
            not SAFE_PATH.fullmatch(relative)
            or any(part in {".", ".."} for part in relative.split("/"))
            or relative == ".bundle-manifest"
        ):
            raise ValueError(f"unsupported bundle path: {relative}")
        metadata = path.lstat()
        payload = None
        if path.is_symlink():
            target = path.readlink().as_posix()
            if not SAFE_PATH.fullmatch(target) or target.startswith("/"):
                raise ValueError(f"unsafe bundle link: {relative}")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(source):
                raise ValueError(f"bundle link escapes source: {relative}")
            canonical = resolved.relative_to(source).as_posix()
            inventory.append(f"L 777 0 {canonical} {relative}\n")
            payload = target.encode("ascii")
            mode = stat.S_IFLNK | 0o777
        elif stat.S_ISDIR(metadata.st_mode):
            inventory.append(f"D 755 0 - {relative}\n")
            payload = b""
            mode = stat.S_IFDIR | 0o755
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            total += metadata.st_size
            if total > MAX_BYTES:
                raise ValueError("bundle is too large")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            permissions = "755" if metadata.st_mode & 0o111 else "644"
            inventory.append(
                f"F {permissions} {metadata.st_size} {digest} {relative}\n"
            )
            mode = stat.S_IFREG | int(permissions, 8)
        else:
            raise ValueError(f"bundle contains a hardlink or special file: {relative}")
        info = zipfile.ZipInfo(
            f"nesk/{relative}" + ("/" if stat.S_ISDIR(mode) else ""), ZIP_TIMESTAMP
        )
        info.create_system = 3
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = mode << 16
        entries.append((path, info, payload))
    manifest = "".join(inventory).encode("ascii")
    if len(manifest) > 4 * 1024**2:
        raise ValueError("bundle inventory is too large")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("nesk/.bundle-manifest", ZIP_TIMESTAMP)
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(info, manifest)
        for path, info, payload in entries:
            if payload is not None:
                archive.writestr(info, payload)
            else:
                with path.open("rb") as reader, archive.open(info, "w") as writer:
                    while chunk := reader.read(256 * 1024):
                        writer.write(chunk)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or Path(argv[1]).suffix != ".zip":
        raise SystemExit("usage: archive_bundle.py SOURCE_DIR OUTPUT.zip")
    archive_bundle(Path(argv[0]), Path(argv[1]))
    print(f"Created {argv[1]}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
