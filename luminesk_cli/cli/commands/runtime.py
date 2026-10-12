from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from luminesk_cli.application.runtime import DockerRuntime
from luminesk_cli.cli.commands.common import emit, parse_inputs, recipe
from luminesk_cli.cli.progress import activity
from luminesk_cli.domain.errors import RuntimeOperationError, ValidationError
from luminesk_cli.domain.manifest import MANIFEST_NAME


def start(namespace: Any) -> int:
    if namespace.attach:
        if namespace.json or namespace.non_interactive:
            raise ValidationError(
                "--attach cannot be combined with --json or --non-interactive"
            )
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValidationError("--attach requires an interactive TTY")
    root = _instance_root(namespace.dir)
    _, manifest = recipe(root)
    values = parse_inputs(manifest, namespace.set, namespace.set_file)
    message = (
        "Preparing the Docker image, starting the server, and waiting for readiness"
        if not namespace.no_wait
        else "Preparing the Docker image and starting the server"
    )
    runtime = DockerRuntime()
    with activity(namespace, message):
        state = runtime.start(
            root,
            input_overrides=values,
            wait_for_readiness=not namespace.no_wait,
        )
    if namespace.attach:
        from luminesk_cli.cli.attach_tui import run_attach_tui

        return run_attach_tui(root, runtime)
    emit(
        namespace,
        {
            "status": state.runtime.status,
            "containerId": state.runtime.container_id,
            "readinessAt": state.last_readiness_at,
        },
        f"Started {state.tag} ({state.runtime.container_id})",
    )
    return 0


def stop(namespace: Any) -> int:
    root = _instance_root(namespace.dir)
    with activity(namespace, "Stopping the server gracefully"):
        state = DockerRuntime().stop(root)
    emit(namespace, {"status": state.runtime.status}, f"Stopped {state.tag}")
    return 0


def restart(namespace: Any) -> int:
    root = _instance_root(namespace.dir)
    _, manifest = recipe(root)
    values = parse_inputs(manifest, namespace.set, namespace.set_file)
    runtime = DockerRuntime()
    with activity(namespace, "Stopping the server for restart") as progress:
        runtime.stop(root)
        progress.update(
            "Preparing the Docker image, starting the server, and waiting for readiness"
            if not namespace.no_wait
            else "Preparing the Docker image and starting the server"
        )
        state = runtime.start(
            root,
            input_overrides=values,
            wait_for_readiness=not namespace.no_wait,
        )
    emit(
        namespace,
        {"status": state.runtime.status, "containerId": state.runtime.container_id},
        f"Restarted {state.tag}",
    )
    return 0


def status(namespace: Any) -> int:
    root = _instance_root(namespace.dir)
    state = DockerRuntime().status(root)
    emit(
        namespace,
        {
            "status": state.runtime.status,
            "containerId": state.runtime.container_id,
            "readinessAt": state.last_readiness_at,
        },
        f"{state.tag}: {state.runtime.status}",
    )
    return 0


def logs(namespace: Any) -> int:
    if namespace.json and namespace.follow:
        raise ValidationError("--json cannot be combined with --follow")

    root = _instance_root(namespace.dir)
    result = DockerRuntime().logs(
        root,
        follow=namespace.follow,
        tail=namespace.tail,
        since=namespace.since,
        timestamps=namespace.timestamps,
    )

    if isinstance(result, int):
        if result != 0:
            raise RuntimeOperationError("Docker log stream failed", exitCode=result)
        return 0

    emit(namespace, {"logs": result}, result, tone="plain")
    return 0


def attach(namespace: Any) -> int:
    if namespace.json or namespace.non_interactive:
        raise ValidationError("attach requires an interactive terminal")

    from luminesk_cli.cli.attach_tui import run_attach_tui

    return run_attach_tui(_instance_root(namespace.dir), DockerRuntime())


def _instance_root(value: str | None) -> Path:
    current = Path(value or ".").expanduser().resolve()

    if value is not None:
        if not (current / MANIFEST_NAME).is_file():
            raise ValidationError(f"{current} is not a Luminesk instance")

        return current

    for candidate in (current, *current.parents):
        if (candidate / MANIFEST_NAME).is_file():
            return candidate

    raise ValidationError(f"no {MANIFEST_NAME} found from {current} to filesystem root")
