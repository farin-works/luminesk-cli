"""Full-screen interactive console for a running Docker instance."""

from __future__ import annotations

import asyncio
import codecs
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from luminesk_cli.application.runtime import (
    MAX_ATTACH_HISTORY_BYTES,
    AttachSession,
    DockerRuntime,
)
from luminesk_cli.cli.console_logs import ConsoleLogBuffer
from luminesk_cli.cli.output import sanitize
from luminesk_cli.cli.theme import color_disabled, prompt_styles, supports_unicode
from luminesk_cli.cli.theme import prompt_color_depth as _color_depth
from luminesk_cli.domain.errors import RuntimeOperationError, ValidationError

MAX_CONSOLE_COMMAND_BYTES = 64 * 1024
DETACH_TIMEOUT_SECONDS = 1.0

# Match explicit logging prefixes, never words in a player's message or command.
LOG_LEVEL = re.compile(
    r"^\s*(?:\[[^\]\n]*\]\s*)?"
    r"(?:\[[^\]\n]*/(?P<thread>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\]"
    r"|\[?(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\]?(?=\s|:|$))"
)


class ConsoleLexer(Lexer):
    """Highlight only severity labels, leaving long log bodies neutral."""

    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        def get_line(index: int) -> StyleAndTextTuples:
            if not 0 <= index < document.line_count:
                return []
            line = document.lines[index]
            match = LOG_LEVEL.match(line)
            if match is None:
                return [("", line)]
            group = "thread" if match["thread"] else "level"
            level = match[group]
            style = (
                "class:log.error"
                if level in {"ERROR", "FATAL"}
                else "class:log.warning"
                if level in {"WARN", "WARNING"}
                else "class:log.debug"
                if level in {"TRACE", "DEBUG"}
                else "class:log.level"
            )
            start, end = match.span(group)
            return [("", line[:start]), (style, line[start:end]), ("", line[end:])]

        return get_line


class _ProcessInput(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


class _ProcessOutput(Protocol):
    async def read(self, size: int = -1, /) -> bytes: ...


class AttachProcess(Protocol):
    @property
    def stdin(self) -> _ProcessInput | None: ...

    @property
    def stdout(self) -> _ProcessOutput | None: ...

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...


ProcessFactory = Callable[[tuple[str, ...]], Awaitable[AttachProcess]]


class _LogControl(UIControl):
    def __init__(self, content: Callable[[], UIContent]) -> None:
        self._content = content

    def create_content(self, width: int, height: int) -> UIContent:
        return self._content()


class AttachTui:
    """Prompt-toolkit application that owns console rendering and key handling."""

    def __init__(
        self,
        root: Path,
        runtime: DockerRuntime,
        session: AttachSession,
        *,
        process_factory: ProcessFactory,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self._root = root
        self._runtime = runtime
        self._session = session
        self._process_factory = process_factory
        self._process: AttachProcess | None = None
        self._closing = False
        self._disconnected = False
        self._status = "Connecting"
        self._stream_error = False
        self._log_buffer = ConsoleLogBuffer(
            MAX_ATTACH_HISTORY_BYTES, color=not color_disabled()
        )
        self._log_buffer.feed(session.history)
        self._log_cursor = self._log_buffer.last_line
        self._follow_output = True

        self._logs = Window(
            content=_LogControl(self._log_content),
            right_margins=[ScrollbarMargin(display_arrows=True)],
            wrap_lines=True,
            style="class:console",
            always_hide_cursor=True,
        )
        self._command = TextArea(
            height=1,
            prompt=[("class:prompt", "❯ " if supports_unicode(sys.stdout) else "> ")],
            multiline=False,
            wrap_lines=False,
            accept_handler=self._accept_command,
            style="class:input",
            input_processors=[
                ConditionalProcessor(
                    AfterInput([("class:input.placeholder", "Type a server command")]),
                    filter=Condition(lambda: not self._command.text),
                )
            ],
        )
        self._bindings = self._create_key_bindings()
        self._application: Application[int] = Application(
            layout=Layout(self._layout(), focused_element=self._command),
            key_bindings=self._bindings,
            full_screen=True,
            min_redraw_interval=1 / 30,
            mouse_support=False,
            style=_style(),
            color_depth=_color_depth(),
            input=input,
            output=output,
        )

    async def run(self) -> int:
        try:
            self._process = await self._process_factory(self._session.argv)
        except OSError as exc:
            raise RuntimeOperationError(f"cannot attach to Docker: {exc}") from exc

        self._status = "Connected | recent history loaded"

        def start_background_tasks() -> None:
            self._application.create_background_task(self._read_console())
            self._application.create_background_task(self._watch_process())

        try:
            result = await self._application.run_async(
                pre_run=start_background_tasks,
                handle_sigint=False,
            )
        finally:
            await self._close_attach_client()

        if result != 0:
            raise RuntimeOperationError("Docker attach failed", exitCode=result)
        return 0

    def _layout(self) -> HSplit:
        return HSplit(
            [
                Window(
                    height=1,
                    content=_dynamic_control(self._header),
                    style="class:header",
                ),
                Window(
                    height=Dimension(min=1, max=3),
                    content=_dynamic_control(self._status_line),
                    wrap_lines=True,
                    dont_extend_height=True,
                    style="class:header",
                ),
                Frame(self._logs, title="Console")
                if supports_unicode(sys.stdout)
                else self._logs,
                Window(
                    height=1,
                    char="─" if supports_unicode(sys.stdout) else "-",
                    style="class:separator",
                ),
                self._command,
                Window(
                    height=Dimension(min=1, max=6),
                    content=_dynamic_control(self._footer),
                    wrap_lines=True,
                    dont_extend_height=True,
                    style="class:footer",
                ),
            ]
        )

    def _header(self) -> StyleAndTextTuples:
        return [
            ("class:title", " Luminesk "),
            ("class:header", f"| {sanitize(self._session.tag)}"),
        ]

    def _status_line(self) -> StyleAndTextTuples:
        if self._status.startswith(
            (
                "Action failed",
                "Cannot send",
                "Docker attach",
                "Command is too large",
                "Killing server",
            )
        ):
            style = "class:status.error"
        elif self._disconnected or self._closing:
            style = "class:status.warning"
        elif self._status.startswith("Connected"):
            style = "class:status.connected"
        else:
            style = "class:status"
        return [(style, f" {sanitize(self._status)}")]

    def _footer(self) -> StyleAndTextTuples:
        bindings = (
            ("class:key", " ^D ", "Detach  "),
            ("class:key.warning", " ^C ", "Stop  "),
            ("class:key.danger", " ^K ", "Kill  "),
            ("class:key", " ^L ", "Clear  "),
            ("class:key", " PgUp/PgDn ", "Scroll  "),
            ("class:key", " End ", "Follow "),
        )
        width = get_app().output.get_size().columns
        fragments: StyleAndTextTuples = []
        used = 0
        for style, key, label in bindings:
            # Keep each ASCII key and its meaning together on narrow terminals.
            size = len(key) + len(label)
            if used and used + size > width:
                fragments.append(("class:footer", "\n"))
                used = 0
            fragments.extend(((style, key), ("class:help", label)))
            used += size
        return fragments

    def _create_key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("c-c", eager=True)
        def stop_server(event: KeyPressEvent) -> None:
            self._schedule_action(event, "Stopping server", self._runtime.stop)

        @bindings.add("c-d", eager=True)
        def detach(event: KeyPressEvent) -> None:
            if self._closing:
                return
            if self._disconnected:
                event.app.exit(result=self._process_exit_code())
                return
            self._closing = True
            self._status = "Detaching"
            event.app.invalidate()
            event.app.create_background_task(self._detach())

        @bindings.add("c-k", eager=True)
        def kill_server(event: KeyPressEvent) -> None:
            self._schedule_action(event, "Killing server", self._runtime.kill)

        @bindings.add("c-l", eager=True)
        def clear_console(event: KeyPressEvent) -> None:
            self._log_buffer.clear()
            self._follow_output = True
            self._logs.vertical_scroll = 0
            event.app.invalidate()

        @bindings.add("pageup", eager=True)
        def page_up(event: KeyPressEvent) -> None:
            render_info = self._logs.render_info
            if render_info is None:
                return
            line = max(0, render_info.first_visible_line() - render_info.window_height)
            self._log_cursor = self._log_buffer.first_line + line
            self._logs.vertical_scroll = line
            self._follow_output = False
            event.app.invalidate()

        @bindings.add("pagedown", eager=True)
        def page_down(event: KeyPressEvent) -> None:
            render_info = self._logs.render_info
            if render_info is None:
                return
            last_line = self._log_buffer.line_count - 1
            line = min(
                last_line,
                render_info.last_visible_line() + render_info.window_height,
            )
            self._log_cursor = self._log_buffer.first_line + line
            self._logs.vertical_scroll = line
            self._follow_output = line == last_line
            event.app.invalidate()

        @bindings.add("end", eager=True)
        def follow_output(event: KeyPressEvent) -> None:
            self._follow_output = True
            event.app.invalidate()

        return bindings

    def _accept_command(self, buffer: Buffer) -> bool:
        command = buffer.text
        buffer.text = ""
        if not command:
            return True
        if len(command.encode("utf-8")) > MAX_CONSOLE_COMMAND_BYTES:
            self._status = "Command is too large (maximum 64 KiB)"
            get_app().invalidate()
            return True
        if self._disconnected or self._closing:
            self._status = "Console is not connected"
            get_app().invalidate()
            return True
        get_app().create_background_task(self._send((command + "\n").encode()))
        return True

    def _schedule_action(
        self,
        event: KeyPressEvent,
        status: str,
        action: Callable[[Path], object],
    ) -> None:
        if self._closing:
            return
        self._closing = True
        self._status = status
        event.app.invalidate()
        event.app.create_background_task(self._run_runtime_action(action))

    async def _run_runtime_action(
        self,
        action: Callable[[Path], object],
    ) -> None:
        try:
            await asyncio.to_thread(action, self._root)
        except Exception as exc:
            self._closing = False
            self._status = f"Action failed: {exc}"
            self._application.invalidate()
            return
        self._application.exit(result=0)

    async def _send(self, data: bytes) -> None:
        process = self._process
        if process is None or process.stdin is None:
            self._status = "Docker attach input is unavailable"
            self._application.invalidate()
            return
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            self._status = f"Cannot send input: {exc}"
            self._application.invalidate()

    async def _detach(self) -> None:
        await self._send(b"\x04")
        process = self._process
        if process is not None:
            try:
                await asyncio.wait_for(process.wait(), DETACH_TIMEOUT_SECONDS)
            except TimeoutError:
                process.terminate()
                await process.wait()
        self._application.exit(result=0)

    async def _read_console(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while chunk := await process.stdout.read(16 * 1024):
                self._append_log(decoder.decode(chunk))
                # A continuously readable pipe must still let key handling run.
                await asyncio.sleep(0)
        except OSError as exc:
            self._stream_error = True
            self._disconnected = True
            self._status = f"Docker attach stream failed: {exc} | press Ctrl+D to exit"
            self._application.invalidate()
        remainder = decoder.decode(b"", final=True)
        if remainder:
            self._append_log(remainder)

    async def _watch_process(self) -> None:
        process = self._process
        if process is None:
            return
        returncode = await process.wait()
        if self._closing or self._stream_error:
            return
        self._disconnected = True
        self._status = (
            "Server console disconnected | press Ctrl+D to exit"
            if returncode == 0
            else f"Docker attach exited with code {returncode} | press Ctrl+D to exit"
        )
        self._application.invalidate()

    def _append_log(self, content: str) -> None:
        if not content:
            return
        first_line = self._log_buffer.first_line
        self._log_buffer.feed(content)
        self._logs.vertical_scroll = max(
            0, self._logs.vertical_scroll - (self._log_buffer.first_line - first_line)
        )
        self._application.invalidate()

    def _log_content(self) -> UIContent:
        logs = self._log_buffer
        if self._follow_output:
            self._log_cursor = logs.last_line
        self._log_cursor = min(logs.last_line, max(logs.first_line, self._log_cursor))
        row = self._log_cursor - logs.first_line

        def get_line(index: int) -> StyleAndTextTuples:
            fragments = logs.line(index)
            if fragments and logs.color and not logs.line_has_sgr(index):
                text = "".join(fragment[1] for fragment in fragments)
                return ConsoleLexer().lex_document(Document(text))(0)
            return fragments

        return UIContent(
            get_line=get_line,
            line_count=logs.line_count,
            cursor_position=Point(
                x=logs.line_length(row) if self._follow_output else 0, y=row
            ),
            show_cursor=False,
        )

    def _process_exit_code(self) -> int:
        if self._stream_error:
            return 1
        process = self._process
        return process.returncode if process is not None and process.returncode else 0

    async def _close_attach_client(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), DETACH_TIMEOUT_SECONDS)
        except TimeoutError:
            return


def run_attach_tui(root: Path, runtime: DockerRuntime) -> int:
    """Prepare and run the full-screen attach console."""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValidationError("attach requires an interactive TTY")
    session = runtime.prepare_attach(root)
    tui = AttachTui(
        root,
        runtime,
        session,
        process_factory=_open_attach_process,
    )
    return asyncio.run(tui.run())


async def _open_attach_process(argv: tuple[str, ...]) -> AttachProcess:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


def _dynamic_control(
    value: Callable[[], StyleAndTextTuples],
):
    from prompt_toolkit.layout.controls import FormattedTextControl

    return FormattedTextControl(value)


def _style() -> Style:
    return Style.from_dict(prompt_styles())
