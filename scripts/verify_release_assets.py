"""Verify the exact release asset set consumed by one-line installers."""

from __future__ import annotations

import hashlib
import re
import stat
import sys
import zipfile
from pathlib import Path

BUNDLE_ASSETS = {
    "luminesk-linux-amd64.zip",
    "luminesk-linux-arm64.zip",
    "luminesk-macos-amd64.zip",
    "luminesk-macos-arm64.zip",
    "luminesk-windows-amd64.zip",
}
CHECKSUM_RE = re.compile(r"^([0-9a-f]{64}) [ *]([^/\\]+)$")


def verify_bundle(path: Path) -> None:
    """Reject truncated or structurally incomplete platform ZIPs before release."""
    executable = "nesk.exe" if "windows" in path.name else "nesk"
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = {member.filename for member in members}
        required = {f"nesk/{executable}", "nesk/_internal/", "nesk/.bundle-manifest"}
        if not required <= names or len(names) != len(members):
            raise SystemExit(f"incomplete or duplicate bundle members: {path.name}")
        if len(members) > 20001 or sum(m.file_size for m in members) > 2 * 1024**3:
            raise SystemExit(f"bundle exceeds extraction limits: {path.name}")
        for member in members:
            parts = member.filename.rstrip("/").split("/")
            if parts[0] != "nesk" or any(
                re.fullmatch(r"[A-Za-z0-9_,+.-]+", part) is None or part in {".", ".."}
                for part in parts
            ):
                raise SystemExit(f"unsafe bundle path: {member.filename}")
            kind = stat.S_IFMT(member.external_attr >> 16)
            if kind not in {stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}:
                raise SystemExit(f"special bundle member: {member.filename}")
            if kind == stat.S_IFLNK and "windows" in path.name:
                raise SystemExit("Windows bundle must not require symlink privileges")
        if archive.getinfo("nesk/.bundle-manifest").file_size > 4 * 1024**2:
            raise SystemExit("bundle inventory exceeds extraction limit")
        if "windows" not in path.name and not (
            archive.getinfo(f"nesk/{executable}").external_attr >> 16 & 0o111
        ):
            raise SystemExit("POSIX bundle entry point must be executable")
        damaged = archive.testzip()
        if damaged is not None:
            raise SystemExit(f"damaged bundle member: {damaged}")


def verify(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise SystemExit(f"release asset directory is invalid: {root}")
    paths = sorted(root.iterdir(), key=lambda path: path.name)
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise SystemExit(f"release asset must be a non-empty regular file: {path}")
    names = {path.name for path in paths}
    required = BUNDLE_ASSETS | {"luminesk-cli.cdx.json"}
    missing = sorted(required - names)
    if missing:
        raise SystemExit(f"release assets are missing: {', '.join(missing)}")

    wheels = sorted(name for name in names if name.endswith(".whl"))
    source_distributions = sorted(
        name
        for name in names
        if name.startswith("luminesk_cli-") and name.endswith(".tar.gz")
    )
    if len(wheels) != 1 or len(source_distributions) != 1:
        raise SystemExit(
            "release assets require exactly one wheel and source distribution"
        )
    allowed = required | set(wheels) | set(source_distributions) | {"SHA256SUMS"}
    unexpected = sorted(names - allowed)
    if unexpected:
        raise SystemExit(f"unexpected release assets: {', '.join(unexpected)}")

    checksum_path = root / "SHA256SUMS"
    checksums: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="ascii").splitlines():
        match = CHECKSUM_RE.fullmatch(line)
        if match is None or match.group(2) in checksums:
            raise SystemExit("SHA256SUMS is malformed or contains duplicate entries")
        checksums[match.group(2)] = match.group(1)
    expected_names = names - {"SHA256SUMS"}
    if set(checksums) != expected_names:
        raise SystemExit("SHA256SUMS does not cover the exact release asset set")
    for name in sorted(expected_names):
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if checksums[name] != actual:
            raise SystemExit(f"release asset checksum mismatch: {name}")
        if name in BUNDLE_ASSETS:
            verify_bundle(root / name)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: verify_release_assets.py DIRECTORY")
    root = Path(argv[0]).resolve()
    verify(root)
    print(
        f"Verified {len(BUNDLE_ASSETS)} onedir bundles, Python artifacts, SBOM, and checksums."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
