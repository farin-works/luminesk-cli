"""Terminal-only progress indicators for blocking human CLI operations."""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from luminesk_cli.domain.progress import ProgressEvent, Stage, observe_progress

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderResult
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.text import Text

STAGES: dict[Stage, str] = {
    "engine": "Connecting to Docker Engine",
    "image-inspect": "Inspecting local OCI image",
    "image-download": "Downloading OCI image layer",
    "image-extract": "Extracting OCI image layer",
    "metadata": "Fetching metadata",
    "resolve": "Resolving versions",
    "download": "Downloading artifacts",
    "image": "Pulling OCI image",
    "verify": "Verifying SHA-256",
    "build": "Building package",
    "start": "Starting server container",
    "ready": "Checking server readiness",
}


class Activity:
    """Present backend telemetry without exposing rendering to handlers."""

    def __init__(self, status: Live | None, message: str = "") -> None:
        self._status = status
        self._message = message
        self._spinner: Spinner | None = None
        if status is not None:
            from rich.spinner import Spinner

            from luminesk_cli.cli.theme import supports_unicode

            self._spinner = Spinner(
                "dots" if supports_unicode(sys.stderr) else "line",
                style="progress.spinner",
            )

    def update(self, message: str) -> None:
        self._message = message
        if self._status is not None:
            self._status.update(_Display(message, None, self._spinner))

    def report(self, event: ProgressEvent) -> None:
        # The operation stays visible when backend sub-stages change.
        if self._status is not None:
            self._status.update(_Display(self._message, event, self._spinner))


class _Display:
    def __init__(
        self, message: str, event: ProgressEvent | None, spinner: Spinner | None
    ) -> None:
        self.message = message
        self.event = event
        self.spinner = spinner

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        from rich.table import Table
        from rich.text import Text

        event = self.event
        if event is not None:
            heading = _status_text(self.message)
            heading.stylize("message.heading")
            yield heading
        detail = _event_text(event) if event is not None else _status_text(self.message)
        determinate = (
            event is not None
            and event.completed is not None
            and event.total is not None
            and 0 <= event.completed <= event.total
            and event.total > 0
        )
        if not determinate:
            row = Table.grid(padding=(0, 1))
            if self.spinner is not None:
                row.add_column(width=2)
                row.add_column()
                row.add_row(self.spinner, detail)
            else:
                row.add_row(detail)
            yield row
            return

        yield detail
        assert event is not None and event.completed is not None and event.total
        # No estimated totals or timer-driven progress. ASCII bars also work in
        # legacy Windows code pages; reserve space for the percentage at any width.
        width = min(40, max(0, options.max_width - 9))
        bar = Text()
        if width:
            complete = width * event.completed // event.total
            bar.append("[", style="progress.track")
            bar.append("=" * complete, style="progress.complete")
            bar.append("-" * (width - complete), style="progress.track")
            bar.append("] ", style="progress.track")
        # Do not round an unfinished transfer up to 100%.
        bar.append(
            f"{event.completed * 100 // event.total:3d}%", style="progress.count"
        )
        yield bar


@contextmanager
def activity(namespace: Any, message: str) -> Generator[Activity]:
    """Show a spinner on interactive stderr and remain silent for automation."""

    if not _enabled(namespace):
        yield Activity(None)
        return

    from rich.live import Live

    status = Live(
        console=_console(),
        transient=True,
        refresh_per_second=10,
        redirect_stdout=False,
        redirect_stderr=False,
    )
    current = Activity(status, message)
    current.update(message)
    status.start(refresh=True)
    try:
        with observe_progress(current.report):
            yield current
    finally:
        status.stop()


def _enabled(namespace: Any) -> bool:
    terminal = (
        not bool(getattr(namespace, "json", False))
        and not bool(getattr(namespace, "non_interactive", False))
        and not bool(getattr(namespace, "debug", False))
        and sys.stderr.isatty()
        and sys.stdout.isatty()
    )
    if not terminal:
        return False
    from luminesk_cli.cli.theme import color_disabled

    return not color_disabled()


def _status_text(message: str) -> Text:
    from rich.text import Text

    from luminesk_cli.cli.output import sanitize

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
    from luminesk_cli.cli.theme import console

    return console(stderr=True)
