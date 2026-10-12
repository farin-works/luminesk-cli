"""Optional, context-local operation telemetry with no presentation dependencies."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

Stage = Literal[
    "engine",
    "image-inspect",
    "image-download",
    "image-extract",
    "metadata",
    "resolve",
    "download",
    "image",
    "verify",
    "build",
    "start",
    "ready",
]


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Only fixed stages and numeric counts; never URLs, paths, or input values."""

    stage: Stage
    completed: int | None = None
    total: int | None = None
    unit: Literal["bytes", "checks", "layers"] = "bytes"


_observer: ContextVar[Callable[[ProgressEvent], None] | None] = ContextVar(
    "progress_observer", default=None
)


@contextmanager
def observe_progress(observer: Callable[[ProgressEvent], None]) -> Generator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def report_progress(
    stage: Stage,
    *,
    completed: int | None = None,
    total: int | None = None,
    unit: Literal["bytes", "checks", "layers"] = "bytes",
) -> None:
    observer = _observer.get()
    if observer is not None:
        observer(ProgressEvent(stage, completed, total, unit))
