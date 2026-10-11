from __future__ import annotations

from argparse import Namespace
from typing import Any

import pytest
from rich.text import Text

from luminesk_cli.cli import progress
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


class FakeStatus:
    def __init__(self, rendered: Text) -> None:
        self.rendered = [rendered]
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def update(self, rendered: Text) -> None:
        self.rendered.append(rendered)

    def stop(self) -> None:
        self.stopped = True


class FakeConsole:
    def __init__(self) -> None:
        self.created: list[tuple[FakeStatus, dict[str, Any]]] = []

    def status(self, rendered: Text, **kwargs: Any) -> FakeStatus:
        status = FakeStatus(rendered)
        self.created.append((status, kwargs))
        return status


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
    assert options["spinner"] in {"dots", "line"}
    assert options["spinner_style"] == "progress.spinner"


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
