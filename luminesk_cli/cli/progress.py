"""Terminal-only progress indicators for blocking human CLI operations."""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.status import Status
from rich.text import Text

from luminesk_cli.cli.output import sanitize
from luminesk_cli.cli.theme import color_disabled, console, supports_unicode
from luminesk_cli.domain.progress import ProgressEvent, Stage, observe_progress

STAGES: dict[Stage, str] = {
    "metadata": "Fetching metadata",
    "resolve": "Resolving versions",
    "download": "Downloading artifacts",
    "image": "Pulling OCI image",
    "verify": "Verifying SHA-256",
    "build": "Building package",
    "start": "Starting server (Docker may pull the locked image)",
    "ready": "Checking server readiness",
}


class Activity:
    """Update one Rich status without exposing it to command handlers."""

    def __init__(self, status: Status | None) -> None:
        self._status = status

    def update(self, message: str) -> None:
        if self._status is not None:
            self._status.update(_status_text(message))

    def report(self, event: ProgressEvent) -> None:
        # Rich controls the refresh rate; every update retains the latest real count.
        if self._status is not None:
            self._status.update(_event_text(event))


@contextmanager
def activity(namespace: Any, message: str) -> Generator[Activity]:
    """Show a spinner on interactive stderr and remain silent for automation."""

    if not _enabled(namespace):
        yield Activity(None)
        return

    status = _console().status(
        _status_text(message),
        spinner="dots" if supports_unicode(sys.stderr) else "line",
        spinner_style="progress.spinner",
    )
    status.start()
    try:
        current = Activity(status)
        with observe_progress(current.report):
            yield current
    finally:
        status.stop()


def _enabled(namespace: Any) -> bool:
    return (
        not bool(getattr(namespace, "json", False))
        and not bool(getattr(namespace, "non_interactive", False))
        and not bool(getattr(namespace, "debug", False))
        and sys.stderr.isatty()
        and sys.stdout.isatty()
        and not color_disabled()
    )


def _status_text(message: str) -> Text:
    return Text(sanitize(message), style="progress.detail")


def _event_text(event: ProgressEvent) -> Text:
    rendered = _status_text(STAGES[event.stage])
    if event.completed is not None:
        rendered.append("  ", style="progress.detail")
        counts = f"{event.completed:,}"
        if event.total is not None and event.total >= event.completed:
            counts += f" / {event.total:,}"
        counts += f" {event.unit}"
        rendered.append(counts, style="message.command")
    return rendered


def _console() -> Console:
    return console(stderr=True)
