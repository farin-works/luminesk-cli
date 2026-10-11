from __future__ import annotations

from argparse import Namespace
from io import StringIO
from typing import Any

import pytest
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from luminesk_cli.cli import progress
from luminesk_cli.cli.theme import THEME
from luminesk_cli.domain.progress import (
    ProgressEvent,
    observe_progress,
    report_progress,
)


@pytest.fixture(autouse=True)
def interactive_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(progress.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("rich.live.Live", FakeStatus)


class FakeStatus:
    def __init__(self, *, console: FakeConsole, **kwargs: Any) -> None:
        console.created.append((self, kwargs))
        self.rendered: list[Text] = []
        self.displays: list[progress._Display] = []
        self.started = False
        self.stopped = False

    def start(self, *, refresh: bool = False) -> None:
        self.started = True

    def update(self, rendered: progress._Display) -> None:
        self.displays.append(rendered)
        self.rendered.append(
            progress._event_text(rendered.event)
            if rendered.event is not None
            else progress._status_text(rendered.message)
        )

    def stop(self) -> None:
        self.stopped = True


class FakeConsole:
    def __init__(self) -> None:
        self.created: list[tuple[FakeStatus, dict[str, Any]]] = []


def _namespace(**overrides: bool) -> Namespace:
    values = {
        "json": False,
        "non_interactive": False,
        "debug": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_activity_starts_updates_and_stops_spinner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = FakeConsole()
    monkeypatch.setattr(progress.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(progress, "_console", lambda: console)

    with progress.activity(_namespace(), "Resolving sources") as current:
        current.update("Building package")

    status, options = console.created[0]
    assert status.started is True
    assert status.stopped is True
    assert [item.plain for item in status.rendered] == [
        "Resolving sources",
        "Building package",
    ]
    assert options["redirect_stdout"] is False
    assert options["redirect_stderr"] is False
    assert options["transient"] is True


def test_activity_stops_spinner_when_operation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = FakeConsole()
    monkeypatch.setattr(progress.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(progress, "_console", lambda: console)

    with pytest.raises(RuntimeError, match="failed"):
        with progress.activity(_namespace(), "Working"):
            raise RuntimeError("failed")

    assert console.created[0][0].stopped is True


@pytest.mark.parametrize(
    "namespace",
    [
        _namespace(json=True),
        _namespace(non_interactive=True),
        _namespace(debug=True),
    ],
)
def test_activity_is_silent_for_automation_and_debug(
    namespace: Namespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(progress.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(
        progress,
        "_console",
        lambda: pytest.fail("disabled activity created a console"),
    )

    with progress.activity(namespace, "Working") as current:
        current.update("Still working")


def test_activity_is_silent_without_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress.sys.stderr, "isatty", lambda: False)
    monkeypatch.setattr(
        progress,
        "_console",
        lambda: pytest.fail("non-TTY activity created a console"),
    )

    with progress.activity(_namespace(), "Working"):
        pass


@pytest.mark.parametrize("environment", [{"NO_COLOR": ""}, {"TERM": "dumb"}])
def test_activity_respects_plain_terminal(
    environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(progress.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(progress, "_console", lambda: pytest.fail("created console"))
    with progress.activity(_namespace(), "Working"):
        report_progress("download", completed=128, total=256)


def test_activity_is_silent_when_only_stdout_is_redirected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(progress.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(progress.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(progress, "_console", lambda: pytest.fail("created console"))
    with progress.activity(_namespace(), "Working"):
        report_progress("image")


def test_activity_renders_real_counts_and_cleans_up_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = FakeConsole()
    monkeypatch.setattr(progress.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(progress, "_console", lambda: console)
    with pytest.raises(RuntimeError):
        with progress.activity(_namespace(), "Resolving"):
            report_progress("metadata")
            report_progress("resolve")
            report_progress("download", completed=1024, total=4096)
            report_progress("download", completed=2048)
            report_progress("image")
            report_progress("verify")
            report_progress("build")
            report_progress("start")
            report_progress("ready", completed=1, total=2, unit="checks")
            raise RuntimeError("failed")
    report_progress("download", completed=9999)
    messages = [item.plain for item in console.created[0][0].rendered]
    assert messages == [
        "Resolving",
        "Fetching metadata",
        "Resolving versions",
        "Downloading artifacts  1,024 / 4,096 bytes",
        "Downloading artifacts  2,048 bytes",
        "Pulling OCI image",
        "Verifying SHA-256",
        "Building package",
        "Starting server container",
        "Checking server readiness  1 / 2 checks",
    ]


def test_progress_observers_are_scoped() -> None:
    outer: list[ProgressEvent] = []
    inner: list[ProgressEvent] = []
    with observe_progress(outer.append):
        report_progress("resolve")
        with observe_progress(inner.append):
            report_progress("download", completed=1)
        report_progress("verify")
    report_progress("build")
    assert [event.stage for event in outer] == ["resolve", "verify"]
    assert inner == [ProgressEvent("download", completed=1)]


@pytest.mark.parametrize("width", [20, 32, 80])
@pytest.mark.parametrize(
    "completed,total,percentage",
    [(0, 4096, "0%"), (1024, 4096, "25%"), (4095, 4096, "99%"), (4096, 4096, "100%")],
)
def test_real_progress_keeps_operation_counts_and_percentage(
    width: int, completed: int, total: int, percentage: str
) -> None:
    stream = StringIO()
    console = Console(file=stream, width=width, theme=THEME, color_system=None)
    display = progress._Display(
        "Applying changes", ProgressEvent("download", completed, total), None
    )
    console.print(display)
    rendered = stream.getvalue()
    assert "Applying changes" in rendered
    assert "Downloading" in rendered
    assert f"{completed:,}" in rendered and f"{total:,}" in rendered
    assert percentage in rendered
    assert all(len(line) <= width for line in rendered.splitlines())


@pytest.mark.parametrize("total", [None, 0, 10])
def test_unknown_or_inconsistent_totals_never_invent_percentage(
    total: int | None,
) -> None:
    stream = StringIO()
    console = Console(file=stream, width=80, theme=THEME, color_system=None)
    console.print(
        progress._Display(
            "Resolving sources",
            ProgressEvent("download", completed=100, total=total),
            Spinner("line", style="progress.spinner"),
        )
    )
    rendered = stream.getvalue()
    assert "Resolving sources" in rendered
    assert "100 bytes" in rendered
    assert "%" not in rendered


def test_determinate_progress_does_not_render_spinner() -> None:
    class UnexpectedSpinner(Spinner):
        def render(self, time: float) -> Text:
            pytest.fail("determinate progress must not animate a spinner")

    Console(file=StringIO(), theme=THEME).print(
        progress._Display(
            "Building package",
            ProgressEvent("download", completed=1, total=2),
            UnexpectedSpinner("line"),
        )
    )


def test_real_live_progress_restores_streams_and_cursor_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(stdout, "isatty", lambda: True)
    monkeypatch.setattr(stderr, "isatty", lambda: True)
    monkeypatch.setattr(progress.sys, "stdout", stdout)
    monkeypatch.setattr(progress.sys, "stderr", stderr)
    monkeypatch.setattr("rich.live.Live", Live)
    console = Console(
        file=stderr, force_terminal=True, color_system="truecolor", theme=THEME
    )
    monkeypatch.setattr(progress, "_console", lambda: console)
    with pytest.raises(RuntimeError, match="backend failed"):
        with progress.activity(_namespace(), "Applying installation"):
            report_progress("download", completed=1024, total=4096)
            assert progress.sys.stdout is stdout
            assert progress.sys.stderr is stderr
            raise RuntimeError("backend failed")
    assert stdout.getvalue() == ""
    assert "25%" in stderr.getvalue()
    assert "\x1b[?25h" in stderr.getvalue()
    before = stderr.getvalue()
    report_progress("verify")
    assert stderr.getvalue() == before
