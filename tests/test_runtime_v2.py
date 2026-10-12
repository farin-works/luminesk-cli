from __future__ import annotations

import csv
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from luminesk_cli.application.runtime import (
    MAX_LOG_OUTPUT_BYTES,
    DockerRuntime,
    build_run_argv,
)
from luminesk_cli.cli import attach_tui
from luminesk_cli.cli.commands import runtime as runtime_commands
from luminesk_cli.cli.entry import main
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
            self.stderr = None
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def kill(self) -> None:
            self.terminated = True

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
        return subprocess.CompletedProcess(argv, 1, "", "No such object: instance")

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

        if argv[1] in {"info", "image"}:
            return subprocess.CompletedProcess(argv, 0, "linux/amd64", "")

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
    assert "--pull=never" in run_call
    assert run_call[run_call.index("--platform") + 1] == "linux/amd64"
    assert events == [
        ProgressEvent("engine"),
        ProgressEvent("image-inspect"),
        ProgressEvent("start"),
        ProgressEvent("ready", completed=0, total=1, unit="checks"),
        ProgressEvent("ready", completed=1, total=1, unit="checks"),
    ]


@pytest.mark.parametrize("already_running", [False, True])
@pytest.mark.parametrize("no_wait", [False, True])
@pytest.mark.parametrize("explicit_dir", [False, True])
def test_start_attach_reuses_runtime_and_running_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    already_running: bool,
    no_wait: bool,
    explicit_dir: bool,
) -> None:
    root = tmp_path / "instance"
    _, initial = prepare_instance(root)
    if already_running:
        initial = replace(
            initial, runtime=RuntimeState(container_id="container-id", status="running")
        )
        write_state(root, initial)
    calls: list[str] = []

    def runner(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv[1])
        output = {
            "info": "linux/amd64",
            "image": "linux/amd64",
            "run": "container-id",
            "inspect": "true",
            "logs": "\x1b[32mDone loading\x1b[0m\n",
        }.get(argv[1], "")
        return subprocess.CompletedProcess(argv, 0, output, "")

    runtime = DockerRuntime(runner)
    monkeypatch.setattr(runtime_commands, "DockerRuntime", lambda: runtime)
    monkeypatch.setattr(runtime_commands.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(runtime_commands.sys.stdout, "isatty", lambda: True)
    monkeypatch.chdir(root)

    def run_tui(attached_root: Path, attached_runtime: DockerRuntime) -> int:
        assert attached_runtime is runtime
        assert attached_root == root.resolve()
        state = load_state(root)
        assert state is not None
        assert state.runtime.container_id == "container-id"
        assert (state.last_readiness_at is not None) == (
            not no_wait and not already_running
        )
        calls.append("attach")
        session = attached_runtime.prepare_attach(root)
        assert session.argv[-1] == "container-id"
        assert "\x1b[32m" in session.history
        return 0

    monkeypatch.setattr(attach_tui, "run_attach_tui", run_tui)
    args = ["start", "--attach"]
    if explicit_dir:
        args += ["--dir", str(root)]
    if no_wait:
        args.append("--no-wait")
    assert main(args) == 0
    assert calls.count("attach") == 1
    if already_running:
        assert calls == ["inspect", "attach", "inspect", "logs"]
        assert load_state(root) == initial
    else:
        assert calls.count("run") == 1
        assert calls.count("logs") == (1 if no_wait else 2)
    assert "stop" not in calls and "kill" not in calls


@pytest.mark.parametrize("failure", ["start", "readiness", "attach", "exited"])
def test_start_attach_failure_preserves_lifecycle_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    root = tmp_path / "instance"
    prepare_instance(root)
    calls: list[str] = []
    attaching = False

    def runner(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv[1])
        if failure == "start" and argv[1] == "run":
            return subprocess.CompletedProcess(argv, 1, "", "start failed")
        output = {
            "info": "linux/amd64",
            "image": "linux/amd64",
            "run": "container-id",
            "inspect": "true",
        }.get(argv[1], "")
        if failure == "exited" and attaching and argv[1] == "inspect":
            output = "false"
        return subprocess.CompletedProcess(argv, 0, output, "")

    runtime = DockerRuntime(runner)

    def ready(*args: object) -> None:
        calls.append("ready")
        if failure == "readiness":
            raise RuntimeOperationError("readiness failed")

    def run_tui(attached_root: Path, attached_runtime: DockerRuntime) -> int:
        nonlocal attaching
        attaching = True
        calls.append("attach")
        attached_runtime.prepare_attach(attached_root)
        raise RuntimeOperationError("attach failed")

    monkeypatch.setattr(runtime, "wait_ready", ready)
    monkeypatch.setattr(runtime_commands, "DockerRuntime", lambda: runtime)
    monkeypatch.setattr(runtime_commands.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(runtime_commands.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(attach_tui, "run_attach_tui", run_tui)
    assert main(["start", "--dir", str(root), "--attach"]) == 8
    stderr = capsys.readouterr().err
    assert ("not running" if failure == "exited" else f"{failure} failed") in stderr
    final = load_state(root)
    assert final is not None
    if failure in {"attach", "exited"}:
        assert "stop" not in calls
        assert calls.count("rm") == 1
        assert final.runtime.status == "running"
    else:
        assert "attach" not in calls
        assert final.runtime.status == "stopped"
        if failure == "readiness":
            assert "stop" in calls
            assert calls.count("rm") == 2


def test_runtime_mount_keeps_unicode_spaces_and_csv_fields(tmp_path: Path) -> None:
    root = tmp_path / "Lumi server, данные"
    lock, state = prepare_instance(root)
    manifest = load_manifest(root / "luminesk.toml")
    argv = build_run_argv(
        root, manifest, lock.runtime.image, "test", state.inputs, target=lock.target
    )
    fields = next(csv.reader([argv[argv.index("--mount") + 1]]))
    assert fields == ["type=bind", f"src={root / 'data'}", "dst=/server/data"]
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert "19132:19132/udp" in argv


def test_status_does_not_mark_a_container_stopped_when_daemon_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instance"
    _, state = prepare_instance(root)
    state = replace(
        state, runtime=RuntimeState(container_id="container-id", status="running")
    )
    write_state(root, state)

    def runner(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 1, "", "Cannot connect to Docker daemon"
        )

    with pytest.raises(RuntimeOperationError, match="engine unavailable"):
        DockerRuntime(runner).status(root)
    assert load_state(root) == state


def test_runtime_pulls_missing_pin_before_run_and_keeps_lock(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    lock, _ = prepare_instance(root)
    original = (root / "luminesk.lock").read_bytes()
    calls: list[tuple[str, ...]] = []
    present = False

    def runner(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal present
        calls.append(tuple(argv))
        output = ""
        if argv[1] == "info":
            output = "linux/amd64"
        elif argv[1] == "image":
            if not present:
                return subprocess.CompletedProcess(argv, 1, "", "No such image")
            output = "linux/amd64"
        elif argv[1] == "pull":
            assert argv[-1] == lock.runtime.image
            present = True
        elif argv[1] == "run":
            assert present
            assert "--pull=never" in argv
            output = "container-id"
        return subprocess.CompletedProcess(argv, 0, output, "")

    DockerRuntime(runner).start(root, wait_for_readiness=False)
    assert [call[1] for call in calls] == [
        "info",
        "image",
        "pull",
        "image",
        "rm",
        "run",
    ]
    assert (root / "luminesk.lock").read_bytes() == original


def test_runtime_rejects_wrong_engine_without_removing_container(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instance"
    _, before = prepare_instance(root)
    calls: list[str] = []

    def runner(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv[1])
        return subprocess.CompletedProcess(argv, 0, "linux/arm64", "")

    with pytest.raises(RuntimeOperationError, match="locked target"):
        DockerRuntime(runner).start(root)
    assert calls == ["info"]
    assert load_state(root) == before


@pytest.mark.parametrize("interrupt", [False, True])
def test_start_timeout_or_interrupt_cleans_up_without_committing(
    tmp_path: Path,
    interrupt: bool,
) -> None:
    root = tmp_path / "instance"
    _, before = prepare_instance(root)
    calls: list[str] = []

    def runner(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv[1])
        if argv[1] == "run":
            if interrupt:
                raise KeyboardInterrupt
            raise subprocess.TimeoutExpired(argv, 60, stderr="secret")
        return subprocess.CompletedProcess(argv, 0, "linux/amd64", "")

    with pytest.raises(KeyboardInterrupt if interrupt else RuntimeOperationError):
        DockerRuntime(runner).start(root)
    assert calls[-2:] == ["run", "rm"]
    assert load_state(root) == before


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
