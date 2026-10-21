from __future__ import annotations

import re
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from typing import Literal

import pytest
from prompt_toolkit.output import ColorDepth
from rich.console import Console

from luminesk_cli.cli import output, theme
from luminesk_cli.cli.attach_tui import _color_depth


@pytest.mark.parametrize("colors", ["standard", "256", "truecolor"])
def test_rich_color_fallbacks_preserve_text(
    colors: Literal["standard", "256", "truecolor"],
) -> None:
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system=colors,
        no_color=False,
        theme=theme.THEME,
        width=32,
    )
    console.print(output.human_message("Warning: review `nesk start`", tone="warning"))
    rendered = stream.getvalue()
    assert "\x1b[" in rendered
    assert (
        re.sub(r"\x1b\[[0-9;]*m", "", rendered).strip()
        == "Warning: review `nesk start`"
    )
    if colors != "truecolor":
        assert "38;2;" not in rendered


@pytest.mark.parametrize(
    "variable,value", [("NO_COLOR", ""), ("NO_COLOR", "1"), ("TERM", "dumb")]
)
def test_no_color_disables_all_rich_ansi(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    stream = StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(theme.sys, "stdout", stream)
    monkeypatch.setenv(variable, value)
    monkeypatch.setenv("FORCE_COLOR", "1")
    output.print_human("Done")
    assert stream.getvalue() == "Done\n"
    assert _color_depth() == ColorDepth.DEPTH_1_BIT


def test_redirected_streams_stay_plain_with_force_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(theme.sys, "stdout", stdout)
    monkeypatch.setattr(theme.sys, "stderr", stderr)
    monkeypatch.setenv("FORCE_COLOR", "1")
    output.print_human("Created `server/world`\n  add  server.jar")
    output.print_error("runtime", "failed\x1b[31m\x9b31m\rspoof")
    assert stdout.getvalue() == "Created `server/world`\n  add  server.jar\n"
    assert stderr.getvalue() == "error [runtime]: failed�[31m�31m�spoof\n"


@pytest.mark.parametrize(
    "control",
    ["\x1b[31m", "\x1b]0;title\x07", "\x9b31m", "\x9d52;c;data\x9c", "\r", "\x08"],
)
def test_sanitization_neutralizes_terminal_controls(control: str) -> None:
    rendered = output.human_message(f"[red]untrusted[/red]{control}", tone="plain")
    assert "[red]untrusted[/red]" in rendered.plain
    assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", rendered.plain)


@pytest.mark.parametrize(
    "environment,expected",
    [
        ({"COLORTERM": "truecolor"}, ColorDepth.DEPTH_24_BIT),
        ({"WT_SESSION": "test"}, ColorDepth.DEPTH_24_BIT),
        ({"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_4_BIT"}, ColorDepth.DEPTH_4_BIT),
        ({}, None),
    ],
)
def test_attach_color_detection(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    expected: ColorDepth | None,
) -> None:
    for name in ("NO_COLOR", "COLORTERM", "WT_SESSION", "PROMPT_TOOLKIT_COLOR_DEPTH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert _color_depth() == expected


def test_ascii_terminal_uses_portable_decorations() -> None:
    with TextIOWrapper(BytesIO(), encoding="ascii") as stream:
        assert not theme.supports_unicode(stream)


@pytest.mark.parametrize("terminal_stream", ["stdout", "stderr"])
def test_color_capability_is_independent_for_each_stream(
    monkeypatch: pytest.MonkeyPatch, terminal_stream: str
) -> None:
    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(theme.sys, "stdout", stdout)
    monkeypatch.setattr(theme.sys, "stderr", stderr)
    monkeypatch.setattr(
        stdout if terminal_stream == "stdout" else stderr, "isatty", lambda: True
    )
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("WT_SESSION", "test")
    monkeypatch.setenv("FORCE_COLOR", "1")
    output.print_human("Installed server")
    output.print_error("runtime", "failed")
    for name, stream in (("stdout", stdout), ("stderr", stderr)):
        assert ("\x1b[" in stream.getvalue()) is (name == terminal_stream)


@pytest.mark.parametrize("hint", [{"COLORTERM": "24bit"}, {"WT_SESSION": "test"}])
def test_both_renderers_honor_true_color_hints(
    monkeypatch: pytest.MonkeyPatch, hint: dict[str, str]
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    for key, value in hint.items():
        monkeypatch.setenv(key, value)
    stream = StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(theme.sys, "stdout", stream)
    assert theme.console().color_system == "truecolor"
    assert theme.prompt_color_depth() == ColorDepth.DEPTH_24_BIT


def test_preview_hierarchy_and_change_semantics_preserve_plain_text() -> None:
    value = (
        "Install preview\nFiles\n"
        "  add server.jar\n  update server.properties\n"
        "  preserve world\n  remove old.jar\n  conflict eula.txt\n"
        "  Runtime command: java -jar server.jar"
    )
    rendered = output.human_message(value, tone="info")
    console = Console(theme=theme.THEME)
    expected = {
        "Files": "message.heading",
        "add": "message.added",
        "update": "message.changed",
        "preserve": "message.muted",
        "remove": "message.removed",
        "conflict": "message.removed",
        "java": "message.command",
    }
    assert rendered.plain == value
    for word, style in expected.items():
        assert rendered.get_style_at_offset(console, value.index(word)).color == (
            console.get_style(style).color
        )


def test_redirected_log_file_contains_only_sanitized_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "server.log"
    monkeypatch.setenv("FORCE_COLOR", "1")
    with path.open("w", encoding="utf-8") as stream:
        monkeypatch.setattr(theme.sys, "stdout", stream)
        output.print_human(
            "[INFO] literal [red]text[/red]\n\x1b[2J\x9d52;c;unsafe\x9c", tone="plain"
        )
    content = path.read_text(encoding="utf-8")
    assert "[INFO] literal [red]text[/red]" in content
    assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", content)
