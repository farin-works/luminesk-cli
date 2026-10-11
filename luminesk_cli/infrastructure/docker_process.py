"""Bounded Docker output capture with interruptible, live progress delivery."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import IO

MAX_OUTPUT = 4 * 1024 * 1024


def run_docker(
    argv: Sequence[str],
    *,
    timeout: float,
    on_line: Callable[[str], None] | None = None,
    output_limit: int | None = None,
    merge_stderr: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Drain both pipes without unbounded buffering or blocking the main thread."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        shell=False,
        bufsize=0,
    )
    assert process.stdout is not None
    streams = [process.stdout]
    if process.stderr is not None:
        streams.append(process.stderr)
    chunks: Queue[tuple[int, bytes | OSError | None]] = Queue(maxsize=16)
    stopped = Event()

    def deliver(index: int, value: bytes | OSError | None) -> None:
        while not stopped.is_set():
            try:
                chunks.put((index, value), timeout=0.1)
                return
            except Full:
                continue

    def read(index: int, stream: IO[bytes]) -> None:
        try:
            while chunk := stream.read(4096):
                deliver(index, chunk)
                if stopped.is_set():
                    return
        except OSError as exc:
            deliver(index, exc)
        finally:
            deliver(index, None)

    readers = [
        Thread(target=read, args=(index, stream), daemon=True)
        for index, stream in enumerate(streams)
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    outputs = [bytearray(), bytearray()]
    pending = [b"", b""]
    finished = 0
    try:
        while finished < len(streams):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                index, chunk = chunks.get(timeout=min(remaining, 0.1))
            except Empty:
                continue
            if chunk is None:
                finished += 1
                continue
            if isinstance(chunk, OSError):
                raise chunk
            output = outputs[index]
            output.extend(chunk)
            if output_limit is not None and sum(map(len, outputs)) > output_limit:
                raise BufferError("Docker output exceeds the configured limit")
            # Retain a bounded diagnostic tail; progress is processed as it arrives.
            if len(output) > MAX_OUTPUT:
                del output[:-MAX_OUTPUT]
            if on_line is not None:
                pending[index] += chunk.replace(b"\r", b"\n")
                lines = pending[index].split(b"\n")
                pending[index] = lines.pop()[-8192:]
                for line in lines:
                    if line:
                        on_line(line.decode("utf-8", errors="replace"))
        if on_line is not None:
            for tail in pending:
                if tail:
                    on_line(tail.decode("utf-8", errors="replace"))
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return subprocess.CompletedProcess(
            argv,
            code,
            outputs[0].decode("utf-8", errors="replace"),
            outputs[1].decode("utf-8", errors="replace"),
        )
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        for reader in readers:
            reader.join(timeout=2)
        for stream in streams:
            stream.close()


def docker_failure(operation: str, output: str) -> str:
    """Classify engine failures without echoing registry URLs or credentials."""
    text = output.lower()
    if any(
        word in text for word in ("no matching manifest", "platform", "exec format")
    ):
        reason = "image does not support the locked container OS/architecture"
    elif any(word in text for word in ("unauthorized", "denied", "authentication")):
        reason = "access denied; check registry login and Docker/file permissions"
    elif any(word in text for word in ("no such host", "dns", "lookup ")):
        reason = "DNS lookup failed; check Docker Desktop DNS, proxy and network"
    elif any(word in text for word in ("timeout", "deadline", "timed out")):
        reason = (
            "network or engine timeout; check Docker Desktop/WSL2 and registry access"
        )
    elif any(
        word in text
        for word in (
            "cannot connect",
            "error during connect",
            "is the docker daemon running",
            "pipe",
            "wsl",
        )
    ):
        reason = (
            "engine unavailable; start Docker Desktop/WSL2 and check the Docker context"
        )
    elif any(word in text for word in ("manifest unknown", "not found")):
        reason = "image or digest is unavailable in the registry"
    elif any(word in text for word in ("certificate", "tls", "proxy")):
        reason = "registry TLS/proxy failure; check Docker trust and proxy settings"
    elif any(word in text for word in ("mount", "sharing")):
        reason = (
            "bind mount failed; check folder sharing and Windows access permissions"
        )
    elif any(word in text for word in ("port is already allocated", "address already")):
        reason = "host port is already in use"
    elif any(
        word in text
        for word in ("connection refused", "network is unreachable", "connection reset")
    ):
        reason = "registry network connection failed; check Docker network and proxy settings"
    else:
        reason = (
            "check Docker Desktop/WSL2, the active context, network and registry access"
        )
    return f"Docker {operation} failed: {reason}"
