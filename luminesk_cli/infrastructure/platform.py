"""Host identification, separate from the Docker execution platform."""

from __future__ import annotations

import platform
import sys

from luminesk_cli.domain.errors import ValidationError

OS_NAMES = {
    "linux": "linux",
    "darwin": "darwin",
    "win32": "windows",
}
ARCH_NAMES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def current_platform() -> str:
    """Return the host platform; never use it to select container artifacts."""
    os_name = OS_NAMES.get(sys.platform)
    machine = platform.machine().lower()
    architecture = ARCH_NAMES.get(machine)

    if os_name is None or architecture is None:
        raise ValidationError(
            f"unsupported platform: {sys.platform}/{machine}",
            os=sys.platform,
            architecture=machine,
        )

    return f"{os_name}/{architecture}"


def container_platform() -> str:
    """Query the active Docker context rather than guessing from the host."""
    from luminesk_cli.infrastructure.oci import OciImageResolver

    return OciImageResolver().target_platform()
