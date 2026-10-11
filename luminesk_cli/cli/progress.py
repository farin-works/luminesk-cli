"""Terminal-only progress indicators for blocking human CLI operations."""

from __future__ import annotations

import random
import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.status import Status
from rich.text import Text

from luminesk_cli.cli.output import THEME, sanitize

TIPS = (
    "This may be a good time to grab an iced tea.",
    "Large images and modpacks can take a little while.",
    "Verification is part of the wait.",
    "Reproducibility takes a moment; future you will appreciate it.",
)


class Activity:
    """Update one Rich status without exposing it to command handlers."""

    def __init__(self, status: Status | None, tip: str) -> None:
        self._status = status
        self._tip = tip

    def update(self, message: str) -> None:
        if self._status is not None:
            self._status.update(_status_text(message, self._tip))


@contextmanager
def activity(namespace: Any, message: str) -> Generator[Activity]:
    """Show a spinner on interactive stderr and remain silent for automation."""

    if not _enabled(namespace):
        yield Activity(None, "")
        return

    tip = random.choice(TIPS)
    status = _console().status(
        _status_text(message, tip),
        spinner="dots",
        spinner_style="message.info",
    )
    status.start()
    try:
        yield Activity(status, tip)
    finally:
        status.stop()


def _enabled(namespace: Any) -> bool:
    return (
        not bool(getattr(namespace, "json", False))
        and not bool(getattr(namespace, "non_interactive", False))
        and not bool(getattr(namespace, "debug", False))
        and sys.stderr.isatty()
    )


def _status_text(message: str, tip: str) -> Text:
    rendered = Text(sanitize(message), style="message.info")
    rendered.append(f" ({sanitize(tip)})", style="message.muted")
    return rendered


def _console() -> Console:
    return Console(stderr=True, theme=THEME, highlight=False)
