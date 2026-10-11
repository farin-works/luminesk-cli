"""Full-screen interactive console for a running Docker instance."""

from __future__ import annotations

import asyncio
import codecs
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from luminesk_cli.application.runtime import (
    MAX_ATTACH_HISTORY_BYTES,
    AttachSession,
    DockerRuntime,
)
from luminesk_cli.cli.output import sanitize
from luminesk_cli.domain.errors import RuntimeOperationError, ValidationError

MAX_CONSOLE_COMMAND_BYTES = 64 * 1024
DETACH_TIMEOUT_SECONDS = 1.0


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
        self._log_text = _clean_console_text(session.history)
        self._follow_output = True

        self._logs = TextArea(
            text=self._log_text,
            read_only=True,
            focusable=False,
            scrollbar=True,
            wrap_lines=True,
            style="class:console",
        )
        self._command = TextArea(
            height=1,
            prompt=[("class:prompt", "❯ ")],
            multiline=False,
            wrap_lines=False,
            accept_handler=self._accept_command,
            style="class:input",
        )
        self._bindings = self._create_key_bindings()
        self._application: Application[int] = Application(
            layout=Layout(self._layout(), focused_element=self._command),
            key_bindings=self._bindings,
            full_screen=True,
            mouse_support=False,
            style=_style(),
            input=input,
            output=output,
        )

    async def run(self) -> int:
        try:
            self._process = await self._process_factory(self._session.argv)
        except OSError as exc:
            raise RuntimeOperationError(f"cannot attach to Docker: {exc}") from exc

        self._status = "Connected · recent history loaded"

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
                Frame(self._logs, title="Console"),
                Window(height=1, char="─", style="class:separator"),
                self._command,
                Window(
                    height=1,
                    content=_dynamic_control(self._footer),
                    style="class:footer",
                ),
            ]
        )

    def _header(self) -> StyleAndTextTuples:
        return [
            ("class:title", f" Luminesk · {sanitize(self._session.tag)} "),
            ("class:status", f" {sanitize(self._status)}"),
        ]

    def _footer(self) -> StyleAndTextTuples:
        return [
            ("class:key", " ^C "),
            ("class:help", "Stop  "),
            ("class:key", " ^D "),
            ("class:help", "Detach  "),
            ("class:key.danger", " ^K "),
            ("class:help", "Kill  "),
            ("class:key", " ^L "),
            ("class:help", "Clear  "),
            ("class:key", " PgUp/PgDn "),
            ("class:help", "Scroll  "),
            ("class:key", " End "),
            ("class:help", "Follow "),
        ]

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
            self._log_text = ""
            self._set_log_document(follow=True)
            event.app.invalidate()

        @bindings.add("pageup", eager=True)
        def page_up(event: KeyPressEvent) -> None:
            render_info = self._logs.window.render_info
            if render_info is None:
                return
            line = max(0, render_info.first_visible_line() - render_info.window_height)
            self._logs.buffer.cursor_position = (
                self._logs.buffer.document.translate_row_col_to_index(line, 0)
            )
            self._logs.window.vertical_scroll = line
            self._follow_output = False
            event.app.invalidate()

        @bindings.add("pagedown", eager=True)
        def page_down(event: KeyPressEvent) -> None:
            render_info = self._logs.window.render_info
            if render_info is None:
                return
            last_line = self._logs.buffer.document.line_count - 1
            line = min(
                last_line,
                render_info.last_visible_line() + render_info.window_height,
            )
            self._logs.buffer.cursor_position = (
                self._logs.buffer.document.translate_row_col_to_index(line, 0)
            )
            self._logs.window.vertical_scroll = line
            self._follow_output = line == last_line
            event.app.invalidate()

        @bindings.add("end", eager=True)
        def follow_output(event: KeyPressEvent) -> None:
            self._follow_output = True
            self._logs.buffer.cursor_position = len(self._log_text)
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
        while chunk := await process.stdout.read(16 * 1024):
            self._append_log(decoder.decode(chunk))
        remainder = decoder.decode(b"", final=True)
        if remainder:
            self._append_log(remainder)

    async def _watch_process(self) -> None:
        process = self._process
        if process is None:
            return
        returncode = await process.wait()
        if self._closing:
            return
        self._disconnected = True
        self._status = (
            "Server console disconnected · press Ctrl+D to exit"
            if returncode == 0
            else f"Docker attach exited with code {returncode} · press Ctrl+D to exit"
        )
        self._application.invalidate()

    def _append_log(self, content: str) -> None:
        if not content:
            return
        self._log_text = _bounded_console_text(
            self._log_text + _clean_console_text(content)
        )
        self._set_log_document(follow=self._follow_output)
        self._application.invalidate()

    def _set_log_document(self, *, follow: bool) -> None:
        cursor_position = (
            len(self._log_text)
            if follow
            else min(self._logs.buffer.cursor_position, len(self._log_text))
        )
        self._logs.buffer.set_document(
            Document(self._log_text, cursor_position=cursor_position),
            bypass_readonly=True,
        )

    def _process_exit_code(self) -> int:
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


def _clean_console_text(content: str) -> str:
    return sanitize(content.replace("\r\n", "\n").replace("\r", "\n"))


def _bounded_console_text(content: str) -> str:
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_ATTACH_HISTORY_BYTES:
        return content
    return encoded[-MAX_ATTACH_HISTORY_BYTES:].decode("utf-8", errors="replace")


def _dynamic_control(
    value: Callable[[], StyleAndTextTuples],
):
    from prompt_toolkit.layout.controls import FormattedTextControl

    return FormattedTextControl(value)


def _style() -> Style:
    return Style.from_dict(
        {
            "header": "bg:#1f2937 #d1d5db",
            "title": "bold #93c5fd",
            "status": "#9ca3af",
            "console": "#d1d5db bg:#111827",
            "separator": "#374151 bg:#111827",
            "input": "#f3f4f6 bg:#111827",
            "prompt": "bold #93c5fd",
            "footer": "bg:#1f2937 #d1d5db",
            "key": "bold #93c5fd bg:#374151",
            "key.danger": "bold #fca5a5 bg:#374151",
            "help": "#d1d5db bg:#1f2937",
        }
    )
