from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from luminesk_cli.application.runtime import (
    MAX_LOG_OUTPUT_BYTES,
    DockerRuntime,
    build_run_argv,
)
from luminesk_cli.domain.errors import RuntimeOperationError, ValidationError
from luminesk_cli.domain.instance import (
    InstanceState,
    RecipeState,
    RuntimeState,
)
from luminesk_cli.domain.lockfile import Lockfile, RuntimeLock, write_lockfile
from luminesk_cli.domain.manifest import Check, load_manifest
from luminesk_cli.domain.progress import ProgressEvent, observe_progress
from luminesk_cli.infrastructure.state import load_state, write_state

MANIFEST = """\
manifest_version = 1
[package]
name = "runtime-fixture"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "bedrock"
[inputs.port]
type = "integer"
default = 19132
[inputs.runtime_uid]
type = "integer"
default = 1000
[inputs.runtime_gid]
type = "integer"
default = 1000
[inputs.data_dir]
type = "string"
default = "data"
pattern = "^[a-z]+$"
[inputs.container_dir]
type = "string"
default = "data"
[inputs.token]
type = "string"
required = true
secret = true
[[sources]]
id = "core"
type = "http"
target = "server.jar"
[sources.options]
url = "https://example.org/server.jar"
[runtime]
image = "example/server:latest"
command = ["java", "-jar", "server.jar; echo not-a-shell"]
workdir = "/server"
memory = "1g"
run_as = "${input.runtime_uid}:${input.runtime_gid}"
read_only_root = true
restart = "on-failure"
restart_limit = 3
[[runtime.mounts]]
source = "${input.data_dir}"
target = "/server/${input.container_dir}"
mode = "rw"
[[runtime.ports]]
name = "bedrock"
host = "${input.port}"
container = "${input.port}"
protocol = "udp"
[[checks]]
id = "ready"
phase = "readiness"
kind = "log-regex"
pattern = "Done"
timeout = 2
"""


def prepare_instance(root: Path) -> tuple[Lockfile, InstanceState]:
    root.mkdir()
    (root / "luminesk.toml").write_text(MANIFEST, encoding="utf-8")
    manifest = load_manifest(root / "luminesk.toml")
    lockfile = Lockfile(
        manifest_digest=manifest.digest,
        target="linux/amd64",
        sources={},
        runtime=RuntimeLock(image=f"example/server@sha256:{'a' * 64}"),
    )
    write_lockfile(root / "luminesk.lock", lockfile)
    state = InstanceState(
        instance_id="12345678-1234-1234-1234-123456789abc",
        name="runtime-fixture",
        tag="runtime-fixture",
        root=str(root),
        applied_lock_digest=lockfile.digest,
        installed_package_digest=f"sha256:{'b' * 64}",
        recipe=RecipeState(),
        inputs={
            "port": 19132,
            "runtime_uid": 1000,
            "runtime_gid": 1000,
            "data_dir": "data",
            "container_dir": "data",
        },
        runtime=RuntimeState(),
        created_at="2026-08-29T00:00:00+00:00",
        updated_at="2026-08-29T00:00:00+00:00",
    )
    write_state(root, state)
    return lockfile, state


def test_runtime_command_keeps_shell_metacharacters_in_one_argv_element(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instance"
    lockfile, _ = prepare_instance(root)
    manifest = load_manifest(root / "luminesk.toml")

    argv = build_run_argv(
        root,
        manifest,
        lockfile.runtime.image,
        "luminesk-fixture",
        {
            "port": 19132,
            "runtime_uid": 1000,
            "runtime_gid": 1000,
            "data_dir": "data",
            "container_dir": "data",
        },
    )

    assert "sh" not in argv
    assert "-c" not in argv
    assert argv[-3:] == (
        "java",
        "-jar",
        "server.jar; echo not-a-shell",
    )
    assert "19132:19132/udp" in argv
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert f"type=bind,src={root / 'data'},dst=/server/data" in argv
    assert lockfile.runtime.image in argv


def test_runtime_mount_input_cannot_escape_instance(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    lockfile, _ = prepare_instance(root)
    manifest = load_manifest(root / "luminesk.toml")

    with pytest.raises(ValidationError, match=r"runtime\.mounts\[0\]\.source"):
        build_run_argv(
            root,
            manifest,
            lockfile.runtime.image,
            "luminesk-fixture",
            {
                "port": 19132,
                "runtime_uid": 1000,
                "runtime_gid": 1000,
                "data_dir": "../escape",
                "container_dir": "data",
            },
        )

    assert not (tmp_path / "escape").exists()


def test_runtime_mount_target_must_remain_canonical_after_input(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instance"
    lockfile, _ = prepare_instance(root)
    manifest = load_manifest(root / "luminesk.toml")

    with pytest.raises(ValidationError, match=r"runtime\.mounts\[0\]\.target"):
        build_run_argv(
            root,
            manifest,
            lockfile.runtime.image,
            "luminesk-fixture",
            {
                "port": 19132,
                "runtime_uid": 1000,
                "runtime_gid": 1000,
                "data_dir": "data",
                "container_dir": "../escape",
            },
        )


def test_follow_logs_failure_uses_stable_runtime_error(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    prepare_instance(root)

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 17, "", "stream failed")

    runtime = DockerRuntime(runner=runner)
    with pytest.raises(RuntimeOperationError) as raised:
        runtime.logs(root, follow=True)

    assert raised.value.code == 8
    assert raised.value.details["exitCode"] == 17


def test_runtime_logs_passes_bounded_filters_to_docker(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    prepare_instance(root)
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "server output\n", "")

    result = DockerRuntime(runner=runner).logs(
        root,
        tail=25,
        since="10m",
        timestamps=True,
    )

    assert result == "server output\n"
    assert calls == [
        (
            "docker",
            "logs",
            "--tail",
            "25",
            "--since",
            "10m",
            "--timestamps",
            "luminesk-runtime-fixture-12345678",
        )
    ]


def test_runtime_logs_rejects_invalid_filters_before_docker(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    prepare_instance(root)
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    runtime = DockerRuntime(runner=runner)
    with pytest.raises(ValidationError, match="between 1 and"):
        runtime.logs(root, tail=0)
    with pytest.raises(ValidationError, match="--since"):
        runtime.logs(root, since="10m\nmalicious")

    assert calls == []


def test_runtime_logs_rejects_oversized_capture(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    prepare_instance(root)

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            "x" * (MAX_LOG_OUTPUT_BYTES + 1),
            "",
        )

    with pytest.raises(RuntimeOperationError, match="reduce --tail"):
        DockerRuntime(runner=runner).logs(root, tail=200)


def test_runtime_logs_stops_production_capture_at_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "instance"
    prepare_instance(root)

    class Pipe:
        def __init__(self) -> None:
            self.read_once = False
            self.closed = False

        def read(self, size: int) -> bytes:
            del size
            if self.read_once:
                return b""
            self.read_once = True
            return b"x" * (MAX_LOG_OUTPUT_BYTES + 1)

        def close(self) -> None:
            self.closed = True

    class Process:
        def __init__(self) -> None:
            self.stdout = Pipe()
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            raise AssertionError("terminated process should not require kill")

        def wait(self, timeout: int | None = None) -> int:
            del timeout
            return 0

    process = Process()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(RuntimeOperationError, match="reduce --tail"):
        DockerRuntime().logs(root, tail=200)

    assert process.terminated is True
    assert process.stdout.closed is True


def test_prepare_attach_loads_bounded_history_and_disables_signal_proxy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instance"
    prepare_instance(root)
    calls = []

    def runner(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 0, "true\n", "")
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 0, "older\nrecent\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    session = DockerRuntime(runner=runner).prepare_attach(root)

    assert session.history == "older\nrecent\n"
    assert session.tag == "runtime-fixture"
    assert session.argv == (
        "docker",
        "attach",
        "--sig-proxy=false",
        "--detach-keys=ctrl-d",
        "luminesk-runtime-fixture-12345678",
    )
    logs_call = next(call for call in calls if call[0][1] == "logs")
    assert logs_call[0][2:4] == ("--tail", "200")
    assert logs_call[1]["stderr"] is subprocess.STDOUT


def test_prepare_attach_rejects_stopped_container(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    prepare_instance(root)

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "false\n", "")

    with pytest.raises(RuntimeOperationError, match="not running"):
        DockerRuntime(runner=runner).prepare_attach(root)


def test_runtime_kill_updates_instance_state(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    _, state = prepare_instance(root)
    running = replace(
        state,
        runtime=RuntimeState(container_id="container-id", status="running"),
    )
    write_state(root, running)
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "container-id\n", "")

    stopped = DockerRuntime(runner=runner).kill(root)

    assert stopped.runtime.status == "stopped"
    assert stopped.runtime.container_id is None
    assert ("docker", "kill", "container-id") in calls
    assert load_state(root) == stopped


def test_runtime_start_records_container_and_readiness(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    prepare_instance(root)
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))

        if argv[1] == "run":
            return subprocess.CompletedProcess(argv, 0, "container-id\n", "")

        if argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 0, "true\n", "")

        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 0, "Done loading\n", "")

        return subprocess.CompletedProcess(argv, 0, "", "")

    events: list[ProgressEvent] = []
    with observe_progress(events.append):
        state = DockerRuntime(runner=runner).start(root)

    assert state.runtime.status == "running"
    assert state.runtime.container_id == "container-id"
    assert state.last_readiness_at is not None
    assert load_state(root) == state
    run_call = next(call for call in calls if call[1] == "run")
    assert run_call[-1] == "server.jar; echo not-a-shell"
    assert events == [
        ProgressEvent("start"),
        ProgressEvent("ready", completed=0, total=1, unit="checks"),
        ProgressEvent("ready", completed=1, total=1, unit="checks"),
    ]


def test_runtime_start_validates_input_overrides_before_docker(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    prepare_instance(root)
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(ValidationError, match="does not match its pattern"):
        DockerRuntime(runner=runner).start(
            root,
            input_overrides={"data_dir": "UPPER"},
            wait_for_readiness=False,
        )

    assert calls == []


def test_command_readiness_runs_argv_inside_container(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    _, state = prepare_instance(root)
    state = replace(
        state,
        runtime=RuntimeState(container_id="container-id", status="running"),
    )
    manifest = load_manifest(root / "luminesk.toml")
    manifest = replace(
        manifest,
        checks=(
            Check(
                id="command-ready",
                phase="readiness",
                kind="command",
                command=("test", "argument;not-shell"),
                timeout=1,
            ),
        ),
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        output = "true\n" if argv[1] == "inspect" else ""
        return subprocess.CompletedProcess(argv, 0, output, "")

    DockerRuntime(runner=runner).wait_ready(root, manifest, state, {})

    assert (
        "docker",
        "exec",
        "container-id",
        "test",
        "argument;not-shell",
    ) in calls


def test_tcp_readiness_cannot_probe_remote_hosts(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    _, state = prepare_instance(root)
    state = replace(
        state,
        runtime=RuntimeState(container_id="container-id", status="running"),
    )
    manifest = load_manifest(root / "luminesk.toml")
    manifest = replace(
        manifest,
        checks=(
            Check(
                id="unsafe-probe",
                phase="readiness",
                kind="tcp",
                host="192.168.1.1",
                port=22,
                timeout=1,
            ),
        ),
    )

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "true\n", "")

    with pytest.raises(ValidationError, match="loopback"):
        DockerRuntime(runner=runner).wait_ready(root, manifest, state, {})
