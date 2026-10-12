"""Fail a release if the wheel ships retired code or a production registry."""

from __future__ import annotations

import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

RETIRED_DIRECTORIES = {
    "bundled_recipes",
    "community_catalog",
    "compatibility_recipes",
    "core",
    "cores",
    "migration",
    "models",
    "utils",
}
RETIRED_DEPENDENCIES = {canonicalize_name("cyclopts")}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: verify_wheel.py DIST.whl")

    wheel = Path(argv[0])

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_names = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]

        if len(metadata_names) != 1:
            raise SystemExit("wheel must contain exactly one METADATA file")

        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        requirements = metadata.get_all("Requires-Dist", [])

    if metadata["Version"] != "2.1.0":
        raise SystemExit(f"unexpected wheel version: {metadata['Version']}")

    try:
        normalized_requirements = {
            canonicalize_name(Requirement(requirement).name)
            for requirement in requirements
        }
    except InvalidRequirement as exc:
        raise SystemExit(f"wheel contains an invalid requirement: {exc}") from exc
    forbidden_dependencies = normalized_requirements & RETIRED_DEPENDENCIES

    if forbidden_dependencies:
        raise SystemExit(
            f"wheel contains retired dependencies: {sorted(forbidden_dependencies)}"
        )

    for directory in RETIRED_DIRECTORIES:
        prefix = f"luminesk_cli/{directory}/"

        if any(name.startswith(prefix) for name in names):
            raise SystemExit(f"wheel contains retired package: {directory}")

    if any(name.endswith((".pyc", ".pyo")) for name in names):
        raise SystemExit("wheel contains bytecode files")

    print(f"Verified {wheel} ({len(names)} members).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
