"""Rich-backed presentation helpers for human-oriented CLI output."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from rich.console import Console
from rich.text import Text

from luminesk_cli.cli.theme import THEME as THEME
from luminesk_cli.cli.theme import console

OutputTone = Literal["success", "info", "warning", "heading", "plain"]

CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
LABEL = re.compile(r"^(\s*)([A-Za-z][A-Za-z0-9 /()_-]*:)(.*)$")
ACTION = re.compile(
    r"^(\s*)(add|added|change|changed|conflict|create|delete|missing|modify|"
    r"preserve|remove|removed|update|write)(\s+)",
    re.IGNORECASE,
)
HEADING = re.compile(r"^[A-Za-z][A-Za-z0-9 /()_-]+$")
MAX_ERROR_DETAIL_LENGTH = 2_000
MAX_ERROR_DETAIL_LINES = 8


def sanitize(value: str) -> str:
    """Remove terminal control sequences from untrusted human output."""

    return CONTROL_CHARACTERS.sub("�", value).replace("\x1b", "�")


def human_message(value: str, *, tone: OutputTone = "success") -> Text:
    """Build safely styled text while preserving the underlying plain message."""

    safe_value = sanitize(value)
    if tone == "plain":
        return Text(safe_value)

    rendered = Text()
    for index, line in enumerate(safe_value.split("\n")):
        if index:
            rendered.append("\n")
        rendered.append_text(_styled_line(line, index=index, tone=tone))
    return rendered


def print_human(value: str, *, tone: OutputTone = "success") -> None:
    """Write human output to stdout with terminal-aware color support."""

    _console().print(human_message(value, tone=tone), soft_wrap=True)


def print_error(
    code: str,
    message: str,
    *,
    details: Mapping[str, object] | None = None,
) -> None:
    """Write a sanitized, consistently styled command error to stderr."""

    rendered = Text()
    rendered.append("error", style="message.error")
    rendered.append(f" [{sanitize(code)}]", style="message.muted")
    rendered.append(": ")
    rendered.append(sanitize(message))
    for line in _safe_error_details(code, details):
        rendered.append("\n  ")
        rendered.append(line, style="message.muted")
    _console(stderr=True).print(rendered, soft_wrap=True)


def print_usage_error(program: str, message: str) -> None:
    """Write an argparse-compatible usage error to stderr."""

    rendered = Text()
    rendered.append(f"{sanitize(program)}: ", style="message.muted")
    rendered.append("error: ", style="message.error")
    rendered.append(sanitize(message))
    _console(stderr=True).print(rendered, soft_wrap=True)


def confirm(question: str) -> bool:
    """Ask a softly styled yes/no question with a safe negative default."""

    prompt = Text(sanitize(question), style="message.prompt")
    prompt.append(" [y/N] ", style="message.muted")
    _console().print(prompt, end="", soft_wrap=True)
    return input().strip().lower() in {"y", "yes"}


def ask(question: str) -> str:
    """Read one safely rendered free-form answer from stdin."""

    prompt = Text(sanitize(question), style="message.prompt")
    prompt.append(" ", style="message.muted")
    _console().print(prompt, end="", soft_wrap=True)
    return input()


def _console(*, stderr: bool = False) -> Console:
    return console(stderr=stderr)


def _safe_error_details(
    code: str,
    details: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """Expose only bounded subprocess diagnostics from trusted error classes."""

    if code not in {"runtime", "transaction"} or details is None:
        return ()
    stderr = details.get("stderr")
    if not isinstance(stderr, str):
        return ()
    safe = sanitize(stderr[-MAX_ERROR_DETAIL_LENGTH:].replace("\r", "\n"))
    lines = [line.strip() for line in safe.splitlines() if line.strip()]
    return tuple(lines[-MAX_ERROR_DETAIL_LINES:])


def _styled_line(line: str, *, index: int, tone: OutputTone) -> Text:
    rendered = Text(line)
    stripped = line.strip()
    if not stripped:
        return rendered

    if index == 0:
        rendered.stylize(f"message.{tone}")
    elif not line[:1].isspace() and HEADING.fullmatch(stripped):
        rendered.stylize("message.heading")

    label = LABEL.match(line)
    if label is not None:
        rendered.stylize("message.label", len(label[1]), len(label[1] + label[2]))
        if label[2].strip().lower() in {"path:", "target:", "directory:"}:
            rendered.stylize("message.path", label.end(2))
        elif label[2].strip().lower() in {"command:", "runtime command:"}:
            rendered.stylize("message.command", label.end(2))

    lowered = stripped.lower()
    if (
        lowered.startswith("warning:")
        or lowered.startswith("warning ")
        or lowered.startswith("security-sensitive ")
    ):
        rendered.stylize("message.warning")

    action = ACTION.match(line)
    if action is not None:
        start = len(action[1])
        style = (
            "message.removed"
            if action[2].lower()
            in {"conflict", "delete", "missing", "remove", "removed"}
            else "message.changed"
            if action[2].lower() in {"change", "changed", "modify", "update"}
            else "message.muted"
            if action[2].lower() == "preserve"
            else "message.added"
        )
        rendered.stylize(style, start, start + len(action[2]))
        rendered.stylize("message.path", action.end())

    marker = len(line) - len(line.lstrip())
    if line[marker : marker + 2] in {"+ ", "++"}:
        rendered.stylize("message.added", marker)
    elif line[marker : marker + 2] in {"- ", "--"}:
        rendered.stylize("message.removed", marker)

    rendered.highlight_regex(r"`[^`]+`", style="message.command")
    rendered.highlight_regex(
        r"\b(?:running|ready|healthy|connected)\b", style="message.success"
    )
    rendered.highlight_regex(
        r"\b(?:stopped|disconnected|pending)\b", style="message.warning"
    )
    rendered.highlight_regex(r"\b(?:failed|unhealthy)\b", style="message.error")
    rendered.highlight_regex(
        r"(?i)\bsha256:[0-9a-f]{16,}\b|\b[0-9a-f]{40,64}\b",
        style="message.muted",
    )
    return rendered
