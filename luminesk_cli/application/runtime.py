"""Manifest-driven single-server Docker runtime with argv-only execution."""

from __future__ import annotations

import csv
import io
import logging
import re
import socket
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from luminesk_cli.domain.errors import (
    ResolutionError,
    RuntimeOperationError,
    ValidationError,
)
from luminesk_cli.domain.inputs import (
    interpolate_input_references,
    resolve_inputs,
    resolve_runtime_port,
)
from luminesk_cli.domain.instance import InstanceState, RuntimeState
from luminesk_cli.domain.lockfile import LOCKFILE_NAME, Lockfile, load_lockfile
from luminesk_cli.domain.manifest import Check, Manifest, RuntimePort, load_manifest
from luminesk_cli.domain.primitives import safe_absolute_posix_path, safe_relative_path
from luminesk_cli.domain.progress import report_progress
from luminesk_cli.infrastructure.docker_process import docker_failure, run_docker
from luminesk_cli.infrastructure.oci import OciImageResolver
from luminesk_cli.infrastructure.recipe_snapshot import load_verified_installed_recipe
from luminesk_cli.infrastructure.state import load_state, write_state

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
MEMORY_RE = re.compile(r"^[1-9][0-9]*(?:[bkmg])?$", re.IGNORECASE)
CONTAINER_NAME_RE = re.compile(r"[^a-z0-9_.-]+")
LOG_SINCE_RE = re.compile(r"^[0-9A-Za-z:+._-]{1,128}$")
LOGGER = logging.getLogger(__name__)
ATTACH_HISTORY_LINES = 200
MAX_ATTACH_HISTORY_BYTES = 1024 * 1024
MAX_LOG_LINES = 100_000
MAX_LOG_OUTPUT_BYTES = 4 * 1024 * 1024


@dataclass(slots=True, frozen=True)
class AttachSession:
    """Verified Docker attach target and its bounded recent console history."""

    argv: tuple[str, ...]
    history: str
    tag: str


class DockerRuntime:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or subprocess.run
        self._bounded_log_capture = runner is None
        self._image_resolver = OciImageResolver(runner)

    def start(
        self,
        root: Path,
        *,
        input_overrides: Mapping[str, str | int | bool] | None = None,
        wait_for_readiness: bool = True,
    ) -> InstanceState:
        root = root.resolve()
        LOGGER.debug(
            "runtime start requested overrides=%d wait_for_readiness=%s",
            len(input_overrides or {}),
            wait_for_readiness,
        )
        state, manifest, lockfile = _load_instance(root)

        if state.pending_transaction is not None:
            raise RuntimeOperationError("cannot start during a pending transaction")

        if state.applied_lock_digest != lockfile.digest:
            raise ValidationError("instance state does not match lockfile")

        if state.runtime.status == "running" and state.runtime.container_id:
            if self.is_running(state.runtime.container_id):
                LOGGER.debug("runtime start skipped already_running=true")
                return state

        values = resolve_inputs(
            manifest,
            {**state.inputs, **(input_overrides or {})},
            require_required=False,
        )
        LOGGER.debug("runtime input validation completed values=%d", len(values))
        try:
            self._image_resolver.ensure_available(
                lockfile.runtime.image, lockfile.target
            )
        except ResolutionError as exc:
            raise RuntimeOperationError(str(exc)) from exc
        container_name = _container_name(state)
        self._run(["docker", "rm", "--force", container_name], check=False)
        command = build_run_argv(
            root,
            manifest,
            lockfile.runtime.image,
            container_name,
            values,
            target=lockfile.target,
        )
        LOGGER.debug(
            "runtime Docker argv prepared mounts=%d ports=%d arguments=%d",
            len(manifest.runtime.mounts) or 1,
            len(manifest.runtime.ports),
            len(command),
        )
        report_progress("start")
        try:
            result = self._run(command, check=False)
        except BaseException:
            # An interrupted/timed-out client may already have created the container.
            try:
                self._run(["docker", "rm", "--force", container_name], check=False)
            except RuntimeOperationError:
                LOGGER.debug("Docker start cleanup failed; container may need removal")
            raise

        if result.returncode != 0:
            raise RuntimeOperationError(
                docker_failure("start", result.stderr or result.stdout),
                exit_code=result.returncode,
            )

        container_id = result.stdout.strip()

        if not container_id:
            raise RuntimeOperationError("Docker returned an empty container id")

        running_state = replace(
            state,
            runtime=RuntimeState(
                driver="docker",
                container_id=container_id,
                status="running",
            ),
            updated_at=datetime.now(UTC).isoformat(),
        )
        write_state(root, running_state)
        LOGGER.debug("runtime state committed status=running")

        try:
            readiness_at = None

            if wait_for_readiness:
                LOGGER.debug("runtime readiness started")
                self.wait_ready(root, manifest, running_state, values)
                readiness_at = datetime.now(UTC).isoformat()

            committed = replace(
                running_state,
                last_readiness_at=readiness_at,
                updated_at=datetime.now(UTC).isoformat(),
            )
            write_state(root, committed)
            LOGGER.debug(
                "runtime start completed readiness_checked=%s",
                readiness_at is not None,
            )
            return committed
        except BaseException as exc:
            LOGGER.debug(
                "runtime start cleanup requested exception_type=%s",
                type(exc).__name__,
            )
            self.stop(root, remove=True)
            raise

    def stop(self, root: Path, *, remove: bool = False) -> InstanceState:
        root = root.resolve()
        LOGGER.debug("runtime stop requested remove=%s", remove)
        state, manifest, _ = _load_instance(root)
        identifier = state.runtime.container_id or _container_name(state)
        result = self._run(
            [
                "docker",
                "stop",
                "--time",
                str(manifest.runtime.stop_timeout),
                identifier,
            ],
            check=False,
            timeout=manifest.runtime.stop_timeout + 30,
        )

        if result.returncode != 0 and self.is_running(identifier):
            raise RuntimeOperationError(
                "Docker failed to stop the instance", stderr=result.stderr[-4000:]
            )

        if remove:
            self._run(["docker", "rm", "--force", identifier], check=False)

        stopped = replace(
            state,
            runtime=RuntimeState(driver="docker", status="stopped"),
            updated_at=datetime.now(UTC).isoformat(),
        )
        write_state(root, stopped)
        LOGGER.debug("runtime stop completed")
        return stopped

    def status(self, root: Path) -> InstanceState:
        root = root.resolve()
        LOGGER.debug("runtime status requested")
        state, _, _ = _load_instance(root)
        identifier = state.runtime.container_id
        running = bool(identifier and self.is_running(identifier))
        actual_status: Literal["running", "stopped"] = (
            "running" if running else "stopped"
        )

        if state.runtime.status == actual_status:
            LOGGER.debug("runtime status unchanged status=%s", actual_status)
            return state

        updated = replace(
            state,
            runtime=RuntimeState(
                driver="docker",
                container_id=identifier if running else None,
                status=actual_status,
            ),
            updated_at=datetime.now(UTC).isoformat(),
        )
        write_state(root, updated)
        LOGGER.debug("runtime status reconciled status=%s", actual_status)
        return updated

    def logs(
        self,
        root: Path,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: str | None = None,
        timestamps: bool = False,
    ) -> int | str:
        _validate_log_options(tail, since)
        LOGGER.debug(
            "runtime logs requested follow=%s tail=%s since=%s timestamps=%s",
            follow,
            tail,
            since is not None,
            timestamps,
        )
        state, _, _ = _load_instance(root.resolve())
        identifier = state.runtime.container_id or _container_name(state)

        if follow:
            argv = _logs_argv(
                identifier,
                follow=True,
                tail=tail,
                since=since,
                timestamps=timestamps,
            )
            try:
                follow_result = self._runner(
                    argv,
                    check=False,
                    shell=False,
                )
            except OSError as exc:
                raise RuntimeOperationError(
                    f"cannot follow Docker logs: {exc}"
                ) from exc
            if follow_result.returncode != 0:
                raise RuntimeOperationError(
                    "cannot follow Docker logs",
                    exitCode=follow_result.returncode,
                )
            return 0

        result = self._read_logs(
            identifier,
            tail=tail,
            since=since,
            timestamps=timestamps,
        )

        if result.returncode != 0:
            raise RuntimeOperationError(
                "cannot read Docker logs",
                stderr=(result.stderr or result.stdout)[-4000:],
            )

        _validate_log_output_size(result.stdout)

        return result.stdout

    def prepare_attach(self, root: Path) -> AttachSession:
        """Resolve a live container and load recent output before opening the TUI."""

        LOGGER.debug("runtime attach preparation requested")
        state, _, _ = _load_instance(root.resolve())
        identifier = state.runtime.container_id or _container_name(state)

        if not self.is_running(identifier):
            raise RuntimeOperationError("instance container is not running")

        result = self._read_logs(identifier, tail=ATTACH_HISTORY_LINES)
        if result.returncode != 0:
            raise RuntimeOperationError(
                "cannot read Docker logs before attach",
                stderr=(result.stderr or result.stdout)[-4000:],
            )

        return AttachSession(
            argv=(
                "docker",
                "attach",
                "--sig-proxy=false",
                "--detach-keys=ctrl-d",
                identifier,
            ),
            history=_bounded_text(result.stdout, MAX_ATTACH_HISTORY_BYTES),
            tag=state.tag,
        )

    def kill(self, root: Path) -> InstanceState:
        """Immediately kill the instance container and reconcile persisted state."""

        root = root.resolve()
        LOGGER.debug("runtime kill requested")
        state, _, _ = _load_instance(root)
        identifier = state.runtime.container_id or _container_name(state)
        result = self._run(["docker", "kill", identifier], check=False)

        if result.returncode != 0 and self.is_running(identifier):
            raise RuntimeOperationError(
                "Docker failed to kill the instance", stderr=result.stderr[-4000:]
            )

        stopped = replace(
            state,
            runtime=RuntimeState(driver="docker", status="stopped"),
            updated_at=datetime.now(UTC).isoformat(),
        )
        write_state(root, stopped)
        LOGGER.debug("runtime kill completed")
        return stopped

    def is_running(self, identifier: str) -> bool:
        result = self._run(
            ["docker", "inspect", "--format", "{{.State.Running}}", identifier],
            check=False,
        )
        if result.returncode:
            diagnostic = (result.stderr or result.stdout).lower()
            if "no such object" in diagnostic or "no such container" in diagnostic:
                return False
            raise RuntimeOperationError(docker_failure("inspect", diagnostic))
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    def wait_ready(
        self,
        root: Path,
        manifest: Manifest,
        state: InstanceState,
        values: Mapping[str, str | int | bool],
    ) -> None:
        checks = [check for check in manifest.checks if check.phase == "readiness"]

        if not checks:
            checks = [
                Check(
                    id="process-alive",
                    phase="readiness",
                    kind="process-alive",
                    timeout=5,
                )
            ]

        LOGGER.debug("runtime readiness checks prepared count=%d", len(checks))
        for index, check in enumerate(checks):
            report_progress("ready", completed=index, total=len(checks), unit="checks")
            LOGGER.debug(
                "runtime readiness check started index=%d kind=%s required=%s",
                index,
                check.kind,
                check.required,
            )
            self._wait_check(root, state, check, values)
            report_progress(
                "ready", completed=index + 1, total=len(checks), unit="checks"
            )
            LOGGER.debug("runtime readiness check completed index=%d", index)

    def check_readiness(self, root: Path) -> InstanceState:
        """Run the declared readiness policy against the live instance."""

        root = root.resolve()
        state, manifest, _ = _load_instance(root)

        if state.runtime.status != "running" or not state.runtime.container_id:
            raise RuntimeOperationError("instance is not running")

        if not self.is_running(state.runtime.container_id):
            raise RuntimeOperationError("instance container is not running")

        self.wait_ready(root, manifest, state, state.inputs)
        checked = replace(
            state,
            last_readiness_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        write_state(root, checked)
        return checked

    def _wait_check(
        self,
        root: Path,
        state: InstanceState,
        check: Check,
        values: Mapping[str, str | int | bool],
    ) -> None:
        identifier = state.runtime.container_id or _container_name(state)
        deadline = time.monotonic() + check.timeout
        last_logs = ""

        while time.monotonic() < deadline:
            if not self.is_running(identifier):
                last_logs = str(self.logs(root, tail=ATTACH_HISTORY_LINES))
                _save_readiness_logs(root, check.id, last_logs)
                raise RuntimeOperationError(
                    f"readiness check {check.id} failed: container stopped"
                )

            if check.kind == "process-alive":
                return

            if check.kind == "log-regex" and check.pattern is not None:
                last_logs = str(self.logs(root, tail=ATTACH_HISTORY_LINES))

                if re.search(check.pattern, last_logs):
                    _save_readiness_logs(root, check.id, last_logs)
                    return

            if check.kind == "tcp" and check.port is not None:
                host = _interpolate(check.host or "127.0.0.1", values)
                _validate_readiness_host(host)
                port = int(_interpolate(str(check.port), values))

                try:
                    with socket.create_connection((host, port), timeout=0.5):
                        return
                except OSError:
                    pass

            if check.kind == "command" and check.command:
                result = self._run(
                    [
                        "docker",
                        "exec",
                        identifier,
                        *(_interpolate(argument, values) for argument in check.command),
                    ],
                    check=False,
                )

                if result.returncode == 0:
                    return

            time.sleep(0.25)

        if check.kind == "log-regex":
            _save_readiness_logs(root, check.id, last_logs)

        if check.required:
            raise RuntimeOperationError(
                f"readiness check {check.id} timed out", timeout=check.timeout
            )

    def _run(
        self,
        argv: Sequence[str],
        *,
        check: bool,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        operation = argv[1] if len(argv) > 1 else "unknown"
        LOGGER.debug(
            "Docker command started operation=%s arguments=%d check=%s",
            operation,
            len(argv),
            check,
        )
        try:
            if self._bounded_log_capture:
                result = run_docker(argv, timeout=timeout)
            else:
                result = self._runner(
                    argv,
                    check=check,
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeOperationError(
                f"Docker {operation} timed out after {timeout:g}s; "
                "check Docker Desktop/WSL2 and the active context",
                operation=operation,
                timeout=timeout,
            ) from exc
        except (OSError, subprocess.CalledProcessError) as exc:
            LOGGER.debug(
                "Docker command raised operation=%s exception_type=%s",
                operation,
                type(exc).__name__,
            )
            raise RuntimeOperationError(docker_failure(operation, "")) from exc
        LOGGER.debug(
            "Docker command completed operation=%s exit_code=%d",
            operation,
            result.returncode,
        )
        return result

    def _read_logs(
        self,
        identifier: str,
        *,
        tail: int | None = None,
        since: str | None = None,
        timestamps: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        argv = _logs_argv(
            identifier,
            follow=False,
            tail=tail,
            since=since,
            timestamps=timestamps,
        )
        if self._bounded_log_capture:
            return _run_bounded_logs(argv)
        try:
            return self._runner(
                argv,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=False,
            )
        except OSError as exc:
            raise RuntimeOperationError(f"cannot read Docker logs: {exc}") from exc


def build_run_argv(
    root: Path,
    manifest: Manifest,
    image: str,
    container_name: str,
    values: Mapping[str, str | int | bool],
    *,
    target: str | None = None,
) -> tuple[str, ...]:
    runtime = manifest.runtime
    command = [
        "docker",
        "run",
        "--detach",
        "--pull=never",
        "--interactive",
        "--name",
        container_name,
        "--workdir",
        runtime.workdir,
        "--stop-signal",
        runtime.stop_signal,
    ]
    if target is not None:
        command.extend(("--platform", target))

    if runtime.memory is not None:
        memory = _interpolate(runtime.memory, values)

        if not MEMORY_RE.fullmatch(memory):
            raise ValidationError("runtime memory value is invalid")

        command.extend(("--memory", memory.lower()))

    if runtime.read_only_root:
        command.append("--read-only")

    if runtime.run_as is not None:
        command.extend(("--user", _interpolate(runtime.run_as, values)))

    restart = runtime.restart

    if restart not in {"no", "on-failure", "always", "unless-stopped"}:
        raise ValidationError(f"unsupported restart policy: {restart}")

    if restart != "no":
        policy = restart

        if restart == "on-failure" and runtime.restart_limit:
            policy = f"{restart}:{runtime.restart_limit}"

        command.extend(("--restart", policy))

    mounts = runtime.mounts

    if not mounts:
        from luminesk_cli.domain.manifest import RuntimeMount

        mounts = (RuntimeMount(source=".", target=runtime.workdir, mode="rw"),)

    for index, mount in enumerate(mounts):
        relative_source = safe_relative_path(
            _interpolate(mount.source, values),
            f"runtime.mounts[{index}].source",
            allow_dot=True,
        )
        source = (root / relative_source).resolve()

        if not source.is_relative_to(root):
            raise ValidationError("runtime mount escapes instance root")

        if relative_source != ".":
            source.mkdir(parents=True, exist_ok=True)

        target = safe_absolute_posix_path(
            _interpolate(mount.target, values),
            f"runtime.mounts[{index}].target",
        )
        # Docker parses --mount as CSV, even when passed as one argv element.
        # Quote whole fields so commas/quotes in a Windows directory stay literal.
        fields = ["type=bind", f"src={source}", f"dst={target}"]
        if mount.mode == "ro":
            fields.append("readonly")
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="").writerow(fields)
        mount_option = buffer.getvalue()

        command.extend(("--mount", mount_option))

    for port in runtime.ports:
        command.extend(("--publish", _port_mapping(port, values)))

    command.append(image)
    command.extend(_interpolate(argument, values) for argument in runtime.command)
    return tuple(command)


def _port_mapping(
    port: RuntimePort,
    values: Mapping[str, str | int | bool],
) -> str:
    host = resolve_runtime_port(port.host, values, name="host")
    container = resolve_runtime_port(port.container, values, name="container")
    return f"{host}:{container}/{port.protocol}"


def _interpolate(value: str, inputs: Mapping[str, str | int | bool]) -> str:
    return interpolate_input_references(value, inputs)


def _validate_readiness_host(host: str) -> None:
    import ipaddress

    if host.lower() == "localhost":
        return

    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValidationError(
            "TCP readiness host must be localhost or a loopback address"
        ) from exc

    if not address.is_loopback:
        raise ValidationError(
            "TCP readiness host must be localhost or a loopback address"
        )


def _load_instance(root: Path) -> tuple[InstanceState, Manifest, Lockfile]:
    state = load_state(root)

    if state is None:
        raise ValidationError("instance state is missing; run nesk install first")

    lockfile = load_lockfile(root / LOCKFILE_NAME)
    if lockfile.recipe is None:
        manifest = load_manifest(root / "luminesk.toml")
        if manifest.digest != lockfile.manifest_digest:
            raise ValidationError("Installed luminesk.toml differs from luminesk.lock.")
    else:
        manifest = load_verified_installed_recipe(root, lockfile).manifest
    return state, manifest, lockfile


def _container_name(state: InstanceState) -> str:
    normalized = CONTAINER_NAME_RE.sub("-", state.tag.lower()).strip("-._")
    return f"luminesk-{normalized[:32]}-{state.instance_id[:8]}"


def _save_readiness_logs(root: Path, check_id: str, content: str) -> None:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = root / ".luminesk_cli" / "logs" / f"readiness-{check_id}-{timestamp}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content[-1024 * 1024 :], encoding="utf-8", errors="replace")


def _bounded_text(content: str, maximum_bytes: int) -> str:
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) <= maximum_bytes:
        return content
    return encoded[-maximum_bytes:].decode("utf-8", errors="replace")


def _validate_log_options(tail: int | None, since: str | None) -> None:
    if tail is not None and (isinstance(tail, bool) or not 1 <= tail <= MAX_LOG_LINES):
        raise ValidationError(f"log tail must be between 1 and {MAX_LOG_LINES}")
    if since is not None and LOG_SINCE_RE.fullmatch(since) is None:
        raise ValidationError(
            "log --since must be a Docker duration, Unix timestamp, or RFC3339 timestamp"
        )


def _logs_argv(
    identifier: str,
    *,
    follow: bool,
    tail: int | None,
    since: str | None,
    timestamps: bool,
) -> tuple[str, ...]:
    argv = ["docker", "logs"]
    if follow:
        argv.append("--follow")
    if tail is not None:
        argv.extend(("--tail", str(tail)))
    if since is not None:
        argv.extend(("--since", since))
    if timestamps:
        argv.append("--timestamps")
    argv.append(identifier)
    return tuple(argv)


def _run_bounded_logs(
    argv: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    try:
        return run_docker(
            argv, timeout=30, output_limit=MAX_LOG_OUTPUT_BYTES, merge_stderr=True
        )
    except BufferError as exc:
        raise _oversized_log_error() from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeOperationError(
            "Docker logs timed out after 30s; check Docker Desktop/WSL2 and the active context"
        ) from exc
    except OSError as exc:
        raise RuntimeOperationError(
            "cannot read Docker logs; check Docker Engine"
        ) from exc


def _validate_log_output_size(output: str) -> None:
    if len(output.encode("utf-8", errors="replace")) > MAX_LOG_OUTPUT_BYTES:
        raise _oversized_log_error()


def _oversized_log_error() -> RuntimeOperationError:
    return RuntimeOperationError(
        "Docker log output exceeds 4 MiB; reduce --tail or narrow --since",
        limit=MAX_LOG_OUTPUT_BYTES,
    )
