"""Incremental, SGR-only console rendering; remote bytes never reach a terminal."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from prompt_toolkit.formatted_text import StyleAndTextTuples

MAX_SEQUENCE_CHARS = 128
MAX_LOG_LINES = 10_000
MAX_LOG_FRAGMENTS = 32_768
MAX_FRAGMENT_CHARS = 4096
_TEXT = re.compile(r"[^\x00-\x1f\x7f-\x9f]+")
_COLORS = (
    "ansiblack",
    "ansired",
    "ansigreen",
    "ansiyellow",
    "ansiblue",
    "ansimagenta",
    "ansicyan",
    "ansigray",
    "ansibrightblack",
    "ansibrightred",
    "ansibrightgreen",
    "ansibrightyellow",
    "ansibrightblue",
    "ansibrightmagenta",
    "ansibrightcyan",
    "ansiwhite",
)


def _indexed_color(value: int) -> str:
    if value < 16:
        return _COLORS[value]
    if value >= 232:
        shade = 8 + 10 * (value - 232)
        return f"#{shade:02x}{shade:02x}{shade:02x}"
    value -= 16
    levels = (0, 95, 135, 175, 215, 255)
    return "#" + "".join(
        f"{levels[index]:02x}" for index in (value // 36, value // 6 % 6, value % 6)
    )


class SgrParser:
    """Carry bounded ANSI state across chunks and emit only text and safe styles."""

    def __init__(self, emit: Callable[[str, str], None], *, color: bool) -> None:
        self._emit = emit
        self._color = color
        self._state = "text"
        self._sequence = ""
        self._valid = True
        self._after_cr = False
        self._attributes: dict[str, str] = {}
        self._style = ""
        self.has_sgr = False

    def feed(self, text: str) -> None:
        index = 0
        while index < len(text):
            if self._state == "text" and (match := _TEXT.match(text, index)):
                self._emit(self._style, match[0])
                self._after_cr = False
                index = match.end()
                continue
            char = text[index]
            index += 1
            if self._state in {"string", "string_escape"}:
                if char in {"\x07", "\x9c"} or (
                    self._state == "string_escape" and char == "\\"
                ):
                    self._state = "text"
                else:
                    self._state = "string_escape" if char == "\x1b" else "string"
                continue
            if char == "\x1b":
                self._state = "escape"
                continue
            if char == "\x9b":
                self._start_csi()
                continue
            if char in {"\x90", "\x98", "\x9d", "\x9e", "\x9f"}:
                self._state = "string"
                continue
            if self._state == "escape":
                if char == "[":
                    self._start_csi()
                elif char in "]PX^_":
                    self._state = "string"
                elif " " <= char <= "/":
                    self._state = "escape_intermediate"
                else:
                    self._state = "text"
                continue
            if self._state == "escape_intermediate":
                if not " " <= char <= "/":
                    self._state = "text"
                continue
            if self._state == "csi":
                if "@" <= char <= "~":
                    if char == "m" and self._valid:
                        self._sgr(self._sequence)
                    self._sequence = ""
                    self._state = "text"
                elif char in "0123456789;" and self._valid:
                    if len(self._sequence) < MAX_SEQUENCE_CHARS:
                        self._sequence += char
                    else:
                        self._sequence = ""
                        self._valid = False
                else:
                    self._valid = False
                    if char in "\n\r":
                        self._state = "text"
                        self._newline(char)
                continue
            if char in "\r\n":
                self._newline(char)
            elif char == "\t":
                self._emit(self._style, "    ")
                self._after_cr = False

    def _newline(self, char: str) -> None:
        if char != "\n" or not self._after_cr:
            self._emit(self._style, "\n")
        self._after_cr = char == "\r"

    def _start_csi(self) -> None:
        self._state = "csi"
        self._sequence = ""
        self._valid = True

    def _sgr(self, sequence: str) -> None:
        values = [int(value or "0") for value in sequence.split(";")]
        self.has_sgr = True
        index = 0
        while index < len(values):
            code = values[index]
            index += 1
            if code == 0:
                self._attributes.clear()
            elif code in {1, 3, 4}:
                name = {1: "bold", 3: "italic", 4: "underline"}[code]
                self._attributes[name] = name
            elif code in {22, 23, 24}:
                self._attributes.pop(
                    {22: "bold", 23: "italic", 24: "underline"}[code], None
                )
            elif code in {39, 49}:
                self._attributes.pop("fg" if code == 39 else "bg", None)
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self._attributes["fg"] = _COLORS[code - (30 if code < 90 else 82)]
            elif 40 <= code <= 47 or 100 <= code <= 107:
                self._attributes["bg"] = _COLORS[code - (40 if code < 100 else 92)]
            elif code in {38, 48}:
                # Consume extended color operands as one unit, never as attributes.
                remaining = values[index:]
                size = 2 if remaining and remaining[0] == 5 else 4
                if not remaining or remaining[0] not in {2, 5} or len(remaining) < size:
                    break
                operands = remaining[1:size]
                index += size
                if all(0 <= value <= 255 for value in operands):
                    color = (
                        _indexed_color(operands[0])
                        if size == 2
                        else "#" + "".join(f"{value:02x}" for value in operands)
                    )
                    self._attributes["fg" if code == 38 else "bg"] = color
            # Dim and other unsupported attributes safely degrade to normal text.
        self._style = (
            " ".join(
                f"{name}:{value}" if name in {"fg", "bg"} else value
                for name, value in self._attributes.items()
            )
            if self._color
            else ""
        )


@dataclass
class _LogLine:
    fragments: deque[tuple[str, str]] = field(default_factory=deque)
    byte_count: int = 0
    length: int = 0
    has_sgr: bool = False
    rendered: StyleAndTextTuples | None = None


class ConsoleLogBuffer:
    """Bound text, line and style metadata independently; render lines on demand."""

    def __init__(self, maximum_bytes: int, *, color: bool = True) -> None:
        self._maximum_bytes = maximum_bytes
        self.color = color
        self.first_line = 0
        self.last_line = 0
        self.byte_count = 0
        self.fragment_count = 0
        self._lines = {0: _LogLine()}
        self._parser = SgrParser(self._append, color=color)

    @property
    def line_count(self) -> int:
        return self.last_line - self.first_line + 1

    def feed(self, text: str) -> None:
        self._parser.feed(text)

    def clear(self) -> None:
        # Clearing the view must not reset an unfinished escape or the server style.
        self.first_line = self.last_line = self.last_line + 1
        self._lines = {self.last_line: _LogLine()}
        self.byte_count = self.fragment_count = 0

    def line(self, index: int) -> StyleAndTextTuples:
        line = self._lines.get(self.first_line + index)
        if line is None:
            return []
        if line.rendered is None:
            line.rendered = list(line.fragments)
        return line.rendered

    def line_has_sgr(self, index: int) -> bool:
        return self._lines[self.first_line + index].has_sgr

    def line_length(self, index: int) -> int:
        return self._lines[self.first_line + index].length

    def _append(self, style: str, text: str) -> None:
        if text == "\n":
            self.byte_count += 1
            self.last_line += 1
            self._lines[self.last_line] = _LogLine()
            self._trim()
            return
        for offset in range(0, len(text), MAX_FRAGMENT_CHARS):
            part = text[offset : offset + MAX_FRAGMENT_CHARS]
            line = self._lines[self.last_line]
            size = len(part.encode("utf-8"))
            if (
                line.fragments
                and line.fragments[-1][0] == style
                and len(line.fragments[-1][1]) + len(part) <= MAX_FRAGMENT_CHARS
            ):
                _, previous = line.fragments.pop()
                line.fragments.append((style, previous + part))
            else:
                line.fragments.append((style, part))
                self.fragment_count += 1
            line.byte_count += size
            line.length += len(part)
            line.has_sgr |= self._parser.has_sgr
            line.rendered = None
            self.byte_count += size
            self._trim()

    def _trim(self) -> None:
        while (
            self.byte_count > self._maximum_bytes
            or self.line_count > MAX_LOG_LINES
            or self.fragment_count > MAX_LOG_FRAGMENTS
        ):
            line = self._lines[self.first_line]
            if self.first_line < self.last_line:
                self.byte_count -= line.byte_count + 1
                self.fragment_count -= len(line.fragments)
                del self._lines[self.first_line]
                self.first_line += 1
            else:
                style, text = line.fragments.popleft()
                size = len(text.encode("utf-8"))
                excess = self.byte_count - self._maximum_bytes
                if self.fragment_count <= MAX_LOG_FRAGMENTS and 0 < excess < size:
                    tail = text.encode("utf-8")[excess:].decode(
                        "utf-8", errors="ignore"
                    )
                    line.fragments.appendleft((style, tail))
                    size -= len(tail.encode("utf-8"))
                    line.length += len(tail)
                    self.fragment_count += 1
                self.byte_count -= size
                self.fragment_count -= 1
                line.byte_count -= size
                line.length -= len(text)
                line.rendered = None
