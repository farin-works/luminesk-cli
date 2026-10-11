"""Luminesk terminal palette, shared by Rich and prompt_toolkit adapters."""

from __future__ import annotations

import os
import sys
from types import MappingProxyType
from typing import TextIO

from rich.console import Console
from rich.theme import Theme

# Keep the brand tokens aligned with docs/src/css/custom.css (dark theme).
PALETTE = MappingProxyType(
    {
        "primary": "#FF0058",
        "primary_dark": "#D6004A",
        "accent": "#FF3379",
        "light_accent": "#FF80A9",
        "background": "#050505",
        "surface": "#0B0B0D",
        "elevated": "#121214",
        "text": "#FFFFFF",
        "secondary": "#B3B3B8",
        "muted": "#7A7A84",
        "success": "#00D26A",
        "warning": "#FFC83D",
        "error": "#FF526D",
    }
)

THEME = Theme(
    {
        "message.success": f"bold {PALETTE['success']}",
        "message.info": f"bold {PALETTE['accent']}",
        "message.warning": f"bold {PALETTE['warning']}",
        "message.error": f"bold {PALETTE['error']}",
        "message.label": PALETTE["secondary"],
        "message.muted": PALETTE["muted"],
        "message.command": PALETTE["light_accent"],
        "message.path": PALETTE["secondary"],
        "message.added": PALETTE["success"],
        "message.removed": PALETTE["error"],
        "message.prompt": f"bold {PALETTE['accent']}",
        "progress.spinner": PALETTE["primary"],
        "progress.detail": PALETTE["secondary"],
    }
)


def color_disabled() -> bool:
    return "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"


def console(*, stderr: bool = False) -> Console:
    """Respect each stream independently, even when FORCE_COLOR is inherited."""

    stream = sys.stderr if stderr else sys.stdout
    enabled = stream.isatty() and not color_disabled()
    return Console(
        file=stream,
        theme=THEME,
        highlight=False,
        force_terminal=None if enabled else False,
        color_system="auto" if enabled else None,
        no_color=not enabled,
    )


def supports_unicode(stream: TextIO) -> bool:
    try:
        "❯─⠋".encode(stream.encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def prompt_styles() -> dict[str, str]:
    """Return toolkit style tokens without loading toolkit on the Rich path."""

    p = PALETTE
    return {
        "": f"{p['text']} bg:{p['background']}",
        "header": f"bg:{p['elevated']} {p['secondary']}",
        "title": f"bold {p['accent']}",
        "status": p["secondary"],
        "status.connected": p["success"],
        "status.warning": p["warning"],
        "status.error": p["error"],
        "console": f"{p['text']} bg:{p['background']}",
        "frame.border": p["muted"],
        "frame.label": p["secondary"],
        "scrollbar.background": f"bg:{p['surface']}",
        "scrollbar.button": f"bg:{p['muted']}",
        "separator": f"{p['primary_dark']} bg:{p['surface']}",
        "input": f"{p['text']} bg:{p['elevated']}",
        "prompt": f"bold {p['primary']}",
        "footer": f"bg:{p['surface']} {p['secondary']}",
        "key": f"bold {p['light_accent']} bg:{p['elevated']}",
        "key.warning": f"bold {p['warning']} bg:{p['elevated']}",
        "key.danger": f"bold {p['error']} bg:{p['elevated']}",
        "help": f"{p['secondary']} bg:{p['surface']}",
    }
