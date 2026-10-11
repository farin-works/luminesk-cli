from __future__ import annotations

import re
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from typing import Literal

import pytest
from prompt_toolkit.output import ColorDepth
from rich.console import Console

from luminesk_cli.cli import output, theme
from luminesk_cli.cli.attach_tui import _color_depth, _style


def test_terminal_palette_matches_website() -> None:
    css = (Path(__file__).parents[1] / "docs/src/css/custom.css").read_text()
    for name, color in theme.PALETTE.items():
        if name != "error":
            assert color in css
    assert theme.THEME.styles["message.success"].color != (
        theme.THEME.styles["message.error"].color
    )
    toolkit = _style()
    assert toolkit.get_attrs_for_style_str("class:title").color == "FF3379"
    assert toolkit.get_attrs_for_style_str("class:prompt").color == "FF0058"
    assert toolkit.get_attrs_for_style_str("class:status.connected").color == "00D26A"
    assert toolkit.get_attrs_for_style_str("class:key.danger").color == "FF526D"
    assert toolkit.get_attrs_for_style_str("class:console").bgcolor == "050505"


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
