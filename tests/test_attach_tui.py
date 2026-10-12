from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import ColorDepth, DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from luminesk_cli.application.runtime import AttachSession, DockerRuntime
from luminesk_cli.cli.attach_tui import (
    AttachTui,
    ConsoleLexer,
    run_attach_tui,
)
from luminesk_cli.cli.console_logs import (
    MAX_LOG_FRAGMENTS,
    MAX_LOG_LINES,
    MAX_SEQUENCE_CHARS,
    ConsoleLogBuffer,
)
from luminesk_cli.domain.errors import RuntimeOperationError, ValidationError


@pytest.mark.parametrize("width", [32, 80])
@pytest.mark.parametrize("no_color", [False, True])
@pytest.mark.parametrize(
    "depth", [ColorDepth.DEPTH_4_BIT, ColorDepth.DEPTH_8_BIT, ColorDepth.DEPTH_24_BIT]
)
def test_attach_layout_keeps_logs_status_and_all_hotkeys_visible(
    tmp_path: Path,
    width: int,
    no_color: bool,
    depth: ColorDepth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("PROMPT_TOOLKIT_COLOR_DEPTH", depth.value)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    stream = StringIO()
    terminal = Vt100_Output(
        stream,
        lambda: Size(rows=24, columns=width),
        term="xterm-256color",
        enable_cpr=False,
    )

    async def scenario() -> list[str]:
        process = FakeProcess(
            [
                b"\x1b[36mReadable log\x1b[m [red]literal[/red]\n"
                b"\x1b[38;2;12;34;56mTruecolor\x1b[m\n\x1b]0;unsafe\x07\n"
            ]
        )

        async def factory(argv: tuple[str, ...]) -> FakeProcess:
            return process

        with create_pipe_input() as pipe_input:
            tui = AttachTui(
                tmp_path,
                DockerRuntime(),
                _session(),
                process_factory=factory,
                input=pipe_input,
                output=terminal,
            )
            task = asyncio.create_task(tui.run())
            await asyncio.sleep(0.1)
            screen = tui._application.renderer._last_screen
            assert screen is not None
            rows = [
                "".join(screen.data_buffer[y][x].char for x in range(width))
                for y in range(24)
            ]
            process.finish(0)
            await asyncio.sleep(0.05)
            assert tui._status_line()[0][0] == "class:status.warning"
            assert "disconnected" in tui._status_line()[0][1]
            pipe_input.send_bytes(b"\x04")
            assert await asyncio.wait_for(task, 2) == 0
        return rows

    rows = asyncio.run(scenario())
    text = "\n".join(rows)
    assert "Luminesk" in text
    assert "Connected" in text
    assert "Readable log" in text
    assert "�" not in text
    assert "[36m" not in text
    assert "unsafe" not in text
    assert "Type a server command" in text
    for key in ("^D", "^C", "^K", "^L", "PgUp/PgDn", "End"):
        assert key in text
    assert "\x1b]0;unsafe" not in stream.getvalue()
    if no_color:
        assert "38;" not in stream.getvalue()
        assert "48;" not in stream.getvalue()
    elif depth == ColorDepth.DEPTH_24_BIT:
        assert "38;2;255;" in stream.getvalue()
        assert "38;2;12;34;56" in stream.getvalue()
    else:
        assert "38;2;" not in stream.getvalue()
        assert "48;2;" not in stream.getvalue()


class FakeProcessOutput:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self._chunks = chunks or []

    async def read(self, size: int = -1) -> bytes:
        del size
        return self._chunks.pop(0) if self._chunks else b""


class FakeProcessInput:
    def __init__(self, process: FakeProcess) -> None:
        self._process = process
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        if data == b"\x04":
            self._process.finish(0)

    async def drain(self) -> None:
        return None


class FakeProcess:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.stdout = FakeProcessOutput(chunks)
        self.returncode: int | None = None
        self._finished = asyncio.Event()
        self.stdin = FakeProcessInput(self)

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.finish(0)

    def finish(self, returncode: int) -> None:
        if self.returncode is None:
            self.returncode = returncode
            self._finished.set()


def _session() -> AttachSession:
    return AttachSession(
        argv=("docker", "attach", "container-id"),
        history="old output\n",
        tag="fixture",
    )


def test_attach_tui_sends_commands_and_detaches_without_stopping_server(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, FakeProcess, list[tuple[str, ...]]]:
        process = FakeProcess([b"new output\n"])
        calls: list[tuple[str, ...]] = []

        async def process_factory(argv: tuple[str, ...]) -> FakeProcess:
            calls.append(argv)
            return process

        with create_pipe_input() as pipe_input:
            tui = AttachTui(
                tmp_path,
                DockerRuntime(),
                _session(),
                process_factory=process_factory,
                input=pipe_input,
                output=DummyOutput(),
            )
            task = asyncio.create_task(tui.run())
            await asyncio.sleep(0.05)
            pipe_input.send_text("say hello\r")
            await asyncio.sleep(0.05)
            pipe_input.send_bytes(b"\x04")
            result = await asyncio.wait_for(task, 2)
        return result, process, calls

    result, process, calls = asyncio.run(scenario())

    assert result == 0
    assert calls == [_session().argv]
    assert process.stdin.writes == [b"say hello\n", b"\x04"]


@pytest.mark.parametrize(
    ("key", "method_name"),
    [(b"\x03", "stop"), (b"\x0b", "kill")],
)
def test_attach_tui_runtime_hotkeys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: bytes,
    method_name: str,
) -> None:
    async def scenario() -> int:
        process = FakeProcess()

        async def process_factory(argv: tuple[str, ...]) -> FakeProcess:
            del argv
            return process

        runtime = DockerRuntime()
        calls: list[Path] = []
        monkeypatch.setattr(
            runtime,
            method_name,
            lambda root, **kwargs: calls.append(root),
        )
        with create_pipe_input() as pipe_input:
            tui = AttachTui(
                tmp_path,
                runtime,
                _session(),
                process_factory=process_factory,
                input=pipe_input,
                output=DummyOutput(),
            )
            task = asyncio.create_task(tui.run())
            await asyncio.sleep(0.05)
            pipe_input.send_bytes(key)
            result = await asyncio.wait_for(task, 2)
        assert calls == [tmp_path]
        return result

    assert asyncio.run(scenario()) == 0


def test_attach_tui_requires_real_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_stream = type("FakeStream", (), {"isatty": lambda self: False})()
    runtime = DockerRuntime()
    monkeypatch.setattr("luminesk_cli.cli.attach_tui.sys.stdin", fake_stream)
    monkeypatch.setattr("luminesk_cli.cli.attach_tui.sys.stdout", fake_stream)

    with pytest.raises(ValidationError, match="interactive TTY"):
        run_attach_tui(tmp_path, runtime)


def test_attach_tui_reports_process_start_failure(tmp_path: Path) -> None:
    async def process_factory(argv: tuple[str, ...]) -> FakeProcess:
        del argv
        raise OSError("docker missing")

    tui = AttachTui(
        tmp_path,
        DockerRuntime(),
        _session(),
        process_factory=process_factory,
        output=DummyOutput(),
    )

    with pytest.raises(RuntimeOperationError, match="cannot attach to Docker"):
        asyncio.run(tui.run())


def test_attach_tui_preserves_nonzero_docker_exit_code(tmp_path: Path) -> None:
    async def scenario() -> None:
        process = FakeProcess()
        process.finish(17)

        async def process_factory(argv: tuple[str, ...]) -> FakeProcess:
            del argv
            return process

        with create_pipe_input() as pipe_input:
            tui = AttachTui(
                tmp_path,
                DockerRuntime(),
                _session(),
                process_factory=process_factory,
                input=pipe_input,
                output=DummyOutput(),
            )
            task = asyncio.create_task(tui.run())
            await asyncio.sleep(0.05)
            pipe_input.send_bytes(b"\x04")
            with pytest.raises(RuntimeOperationError) as raised:
                await asyncio.wait_for(task, 2)
        assert raised.value.details["exitCode"] == 17

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "line,level,style",
    [
        ("[12:34:56] [Server thread/WARN]: Can't keep up!", "WARN", "log.warning"),
        ("[12:34:56] [Server thread/ERROR]: Load failed", "ERROR", "log.error"),
        ("[INFO] Loaded world", "INFO", "log.level"),
        ("DEBUG: Looking for config", "DEBUG", "log.debug"),
        ("WARNING: Low memory", "WARNING", "log.warning"),
        ("[FATAL] Server exited", "FATAL", "log.error"),
        ("[TRACE] Loading", "TRACE", "log.debug"),
    ],
)
def test_log_severity_styles_only_the_level(line: str, level: str, style: str) -> None:
    fragments = ConsoleLexer().lex_document(Document(line))(0)
    assert "".join(fragment[1] for fragment in fragments) == line
    assert [(fragment[0], fragment[1]) for fragment in fragments if fragment[0]] == [
        (f"class:{style}", level)
    ]


@pytest.mark.parametrize(
    "line", ["player: ERROR is my name", "[INFO] player said ERROR", "WARNINGS.txt"]
)
def test_log_body_does_not_create_false_error_highlights(line: str) -> None:
    fragments = ConsoleLexer().lex_document(Document(line))(0)
    assert all(
        fragment[0] not in {"class:log.error", "class:log.warning"}
        for fragment in fragments
    )


def _log_text(logs: ConsoleLogBuffer) -> str:
    return "\n".join(
        "".join(fragment[1] for fragment in logs.line(index))
        for index in range(logs.line_count)
    )


def test_log_sgr_preserves_server_colors_and_resets() -> None:
    logs = ConsoleLogBuffer(1024)
    logs.feed("\x1b[36m14:33:54\x1b[m [\x1b[32mINFO\x1b[0m] ")
    logs.feed("\x1b[33mWARN\x1b[31mERROR\x1b[39m normal")
    assert logs.line(0) == [
        ("fg:ansicyan", "14:33:54"),
        ("", " ["),
        ("fg:ansigreen", "INFO"),
        ("", "] "),
        ("fg:ansiyellow", "WARN"),
        ("fg:ansired", "ERROR"),
        ("", " normal"),
    ]
    assert logs.line(1) == []


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("index", range(16))
def test_log_standard_and_bright_colors(index: int, background: bool) -> None:
    colors = (
        "ansiblack ansired ansigreen ansiyellow ansiblue ansimagenta ansicyan "
        "ansigray ansibrightblack ansibrightred ansibrightgreen ansibrightyellow "
        "ansibrightblue ansibrightmagenta ansibrightcyan ansiwhite"
    ).split()
    code = (30 if index < 8 else 90) + index % 8 + (10 if background else 0)
    logs = ConsoleLogBuffer(1024)
    logs.feed(f"\x1b[{code}mcolor\x1b[{49 if background else 39}mreset")
    assert logs.line(0) == [
        (f"{'bg' if background else 'fg'}:{colors[index]}", "color"),
        ("", "reset"),
    ]


@pytest.mark.parametrize(
    "sequence,style",
    [
        ("38;5;0", "fg:ansiblack"),
        ("38;5;9", "fg:ansibrightred"),
        ("38;5;16", "fg:#000000"),
        ("38;5;196", "fg:#ff0000"),
        ("48;5;231", "bg:#ffffff"),
        ("48;5;232", "bg:#080808"),
        ("38;5;255", "fg:#eeeeee"),
        ("38;2;12;34;56", "fg:#0c2238"),
        ("48;2;255;0;128", "bg:#ff0080"),
        ("1;2;3;4", "bold italic underline"),
        ("1;3;4;22;23;24", ""),
        ("2;22", ""),
        ("38;5;256", ""),
        ("38;2;256;1;2", ""),
        ("38;2;1", ""),
        ("38;7;1", ""),
        ("38", ""),
        ("5;6;7;8;9", ""),
    ],
)
def test_log_extended_colors_and_safe_attribute_degradation(
    sequence: str, style: str
) -> None:
    logs = ConsoleLogBuffer(1024)
    logs.feed(f"\x1b[{sequence}mvalue")
    assert logs.line(0) == [(style, "value")]


@pytest.mark.parametrize("split", range(1, 35))
def test_log_chunk_boundaries_preserve_ansi_unicode_and_crlf(split: int) -> None:
    content = "\x1b[38;2;10;20;30mПривет🙂\r\nмир\rnext\x1b[m!"
    logs = ConsoleLogBuffer(1024)
    logs.feed(content[:split])
    logs.feed(content[split:])
    assert _log_text(logs) == "Привет🙂\nмир\nnext!"
    assert logs.line(0) == [("fg:#0a141e", "Привет🙂")]
    assert logs.line(1) == [("fg:#0a141e", "мир")]
    assert logs.line(2) == [("fg:#0a141e", "next"), ("", "!")]


@pytest.mark.parametrize(
    "sequence",
    [
        "\x1b]52;c;clipboard\x07",
        "\x1b]0;title\x1b\\",
        "\x1b]8;;https://evil.invalid\x1b\\",
        "\x1b[2J",
        "\x1b[999C",
        "\x1b[?1049h",
        "\x1b[8;999;999t",
        "\x1b[31:1m",
        "\x1b[1\x00m",
        "\x1b(0",
        "\x1b7",
        "\x1bPpayload\x1b\\",
        "\x1b_payload\x1b\\",
        "\x1b^payload\x1b\\",
        "\x1bXpayload\x1b\\",
        "\x9d52;c;clipboard\x9c",
        "\x90payload\x9c",
        "\x9b2J",
        "\x00\x01\x02\x07\x08\x7f\x85",
        "\x1b[" + "9" * 10000 + "m",
        "\x1b]52;c;" + "x" * 10000 + "\x07",
    ],
)
def test_log_control_injections_are_discarded_across_chunks(sequence: str) -> None:
    logs = ConsoleLogBuffer(1024)
    logs.feed("before")
    for char in sequence:
        logs.feed(char)
    logs.feed("after")
    assert _log_text(logs) == "beforeafter"
    assert all(not fragment[0] for fragment in logs.line(0))


def test_log_unfinished_sequences_and_clear_stay_bounded() -> None:
    logs = ConsoleLogBuffer(1024)
    logs.feed("\x1b[31mred\x1b]52;c;")
    logs.clear()
    for _ in range(100):
        logs.feed("x" * 10000)
    assert logs.byte_count == 0
    logs.feed("\x07still red\x1b[")
    logs.clear()
    logs.feed("32mgreen")
    assert logs.line(0) == [("fg:ansigreen", "green")]
    logs.feed("\x1b[" + "9" * (MAX_SEQUENCE_CHARS + 1))
    assert len(logs._parser._sequence) <= MAX_SEQUENCE_CHARS
    logs.feed("mreadable")
    assert _log_text(logs) == "greenreadable"


def test_log_storage_bounds_bytes_lines_fragments_and_unicode() -> None:
    logs = ConsoleLogBuffer(64)
    logs.feed("🙂" * 100)
    assert _log_text(logs) == "🙂" * 16
    logs.feed("\x1b[31m" + "я" * 100)
    assert logs.byte_count <= 64
    assert "�" not in _log_text(logs)
    assert logs.line(0)[-1][0] == "fg:ansired"

    logs = ConsoleLogBuffer(1024 * 1024)
    logs.feed("line\n" * (MAX_LOG_LINES + 100))
    assert logs.line_count == MAX_LOG_LINES
    logs.clear()
    logs.feed("\x1b[31mx\x1b[32my" * MAX_LOG_FRAGMENTS)
    assert logs.fragment_count <= MAX_LOG_FRAGMENTS
    assert logs.byte_count <= 1024 * 1024


def test_log_completed_lines_are_reused_during_streaming() -> None:
    logs = ConsoleLogBuffer(1024 * 1024)
    logs.feed("\x1b[32mhistory\n")
    history = logs.line(0)
    for _ in range(1000):
        logs.feed("x")
    assert logs.line(0) is history
    assert logs.fragment_count == 2


@pytest.mark.parametrize("no_color", [False, True])
def test_tui_decodes_split_utf8_and_history_ansi_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_color: bool
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")

    async def scenario() -> None:
        content = "mПривет🙂\r\n\x1b[0mnormal".encode()
        process = FakeProcess([bytes([byte]) for byte in content])

        async def factory(argv: tuple[str, ...]) -> FakeProcess:
            return process

        with create_pipe_input() as pipe_input:
            tui = AttachTui(
                tmp_path,
                DockerRuntime(),
                AttachSession(argv=_session().argv, tag="fixture", history="\x1b[36"),
                process_factory=factory,
                input=pipe_input,
                output=DummyOutput(),
            )
            tui._process = process
            await tui._read_console()
            assert _log_text(tui._log_buffer) == "Привет🙂\nnormal"
            assert tui._log_content().get_line(0) == [
                ("" if no_color else "fg:ansicyan", "Привет🙂")
            ]
            assert tui._log_content().get_line(1) == [("", "normal")]
            assert tui._header()[0][0] == "class:title"
            assert tui._command.buffer.text == ""

    asyncio.run(scenario())


def test_tui_scroll_follow_clear_and_resize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)

    async def scenario() -> None:
        process = FakeProcess()
        size = Size(rows=24, columns=80)
        output = Vt100_Output(StringIO(), lambda: size, enable_cpr=False)

        async def factory(argv: tuple[str, ...]) -> FakeProcess:
            return process

        with create_pipe_input() as pipe_input:
            tui = AttachTui(
                tmp_path,
                DockerRuntime(),
                _session(),
                process_factory=factory,
                input=pipe_input,
                output=output,
            )
            tui._append_log("\x1b[32m" + "".join(f"line {i}\n" for i in range(200)))
            task = asyncio.create_task(tui.run())
            await asyncio.sleep(0.1)
            assert (
                tui._log_content().cursor_position.y == tui._log_buffer.line_count - 1
            )
            pipe_input.send_bytes(b"\x1b[5~")
            await asyncio.sleep(0.1)
            assert not tui._follow_output
            anchor = tui._log_cursor
            tui._append_log("new live output\n")
            assert (
                tui._log_content().cursor_position.y
                == anchor - tui._log_buffer.first_line
            )
            pipe_input.send_bytes(b"\x1b[6~")
            await asyncio.sleep(0.1)
            assert tui._log_cursor > anchor
            pipe_input.send_bytes(b"\x1b[F")
            await asyncio.sleep(0.1)
            assert tui._follow_output
            size = Size(rows=18, columns=32)
            tui._application.invalidate()
            await asyncio.sleep(0.1)
            assert tui._logs.render_info is not None
            assert tui._logs.render_info.window_width < 32
            pipe_input.send_bytes(b"\x0c")
            await asyncio.sleep(0.1)
            assert _log_text(tui._log_buffer) == ""
            assert tui._follow_output
            tui._append_log("after clear")
            assert tui._log_content().get_line(0) == [("fg:ansigreen", "after clear")]
            pipe_input.send_bytes(b"\x04")
            assert await asyncio.wait_for(task, 2) == 0

    asyncio.run(scenario())


def test_tui_stream_error_reports_failure_and_allows_exit(tmp_path: Path) -> None:
    class BrokenOutput(FakeProcessOutput):
        async def read(self, size: int = -1) -> bytes:
            raise ConnectionResetError("connection lost")

    async def scenario() -> None:
        process = FakeProcess()
        process.stdout = BrokenOutput()

        async def factory(argv: tuple[str, ...]) -> FakeProcess:
            return process

        with create_pipe_input() as pipe_input:
            tui = AttachTui(
                tmp_path,
                DockerRuntime(),
                _session(),
                process_factory=factory,
                input=pipe_input,
                output=DummyOutput(),
            )
            task = asyncio.create_task(tui.run())
            await asyncio.sleep(0.1)
            assert "connection lost" in tui._status_line()[0][1]
            pipe_input.send_bytes(b"\x04")
            with pytest.raises(RuntimeOperationError, match="Docker attach failed"):
                await asyncio.wait_for(task, 2)
            assert process.returncode == 0

    asyncio.run(scenario())
