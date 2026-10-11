from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from luminesk_cli.application.locking import LockService
from luminesk_cli.cli.commands.common import resolve_lock, validate_frozen_lock
from luminesk_cli.domain.errors import ResolutionError, ValidationError
from luminesk_cli.domain.lockfile import parse_lockfile, write_lockfile
from luminesk_cli.domain.manifest import load_manifest
from luminesk_cli.domain.progress import ProgressEvent, observe_progress
from luminesk_cli.infrastructure import oci, platform
from luminesk_cli.infrastructure.cache import ContentCache
from luminesk_cli.infrastructure.docker_process import MAX_OUTPUT, run_docker
from luminesk_cli.infrastructure.oci import (
    ENGINE_TIMEOUT,
    PULL_TIMEOUT,
    OciImageResolver,
)

PIN = f"example/server@sha256:{'a' * 64}"


class Engine:
    def __init__(self, target: str = "linux/amd64", *, present: bool = False) -> None:
        self.target = target
        self.present = present
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Sequence[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        assert options["shell"] is False
        assert options["timeout"] == (
            PULL_TIMEOUT if argv[1] == "pull" else ENGINE_TIMEOUT
        )
        output = ""
        if argv[1] == "info":
            output = self.target
        elif argv[1] == "image":
            if not self.present:
                return subprocess.CompletedProcess(argv, 1, "", "No such image")
            output = (
                self.target if argv[-1] == "{{.Os}}/{{.Architecture}}" else f'["{PIN}"]'
            )
        elif argv[1] == "pull":
            assert argv[2:4] == ["--platform", self.target]
            self.present = True
            output = (
                "aabbccddeeff: Downloading\n"
                "aabbccddeeff: Extracting\n"
                "aabbccddeeff: Pull complete\n"
                "aabbccddeeff: Pull complete\n"
                "ffeeddccbbaa: Already exists\n"
                "https://user:secret@registry.example/?token=secret\n"
            )
        else:
            pytest.fail(f"unexpected Docker operation: {argv[1]}")
        return subprocess.CompletedProcess(argv, 0, output, "")


@pytest.mark.parametrize(
    ("host_os", "host_arch", "engine", "target"),
    [
        ("win32", "AMD64", "linux/x86_64", "linux/amd64"),
        ("win32", "ARM64", "linux/aarch64", "linux/arm64"),
        ("win32", "AMD64", "linux/arm64", "linux/arm64"),
        ("win32", "ARM64", "linux/amd64", "linux/amd64"),
        ("linux", "x86_64", "linux/x86_64", "linux/amd64"),
        ("darwin", "arm64", "linux/aarch64", "linux/arm64"),
    ],
)
def test_lock_selects_engine_platform_and_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_os: str,
    host_arch: str,
    engine: str,
    target: str,
) -> None:
    monkeypatch.setattr(platform.sys, "platform", host_os)
    monkeypatch.setattr(platform.platform, "machine", lambda: host_arch)
    assert platform.current_platform().split("/")[0] == platform.OS_NAMES[host_os]
    (tmp_path / "core").write_bytes(b"core")
    recipe = tmp_path / "luminesk.toml"
    recipe.write_text(
        f'''manifest_version = 1
[package]
name = "platform-fixture"
version = "1.0.0"
kind = "core"
game = "minecraft"
edition = "bedrock"
platforms = ["linux/amd64", "linux/arm64"]
[[sources]]
id = "amd"
type = "local-file"
target = "server.bin"
platforms = ["linux/amd64"]
[sources.options]
path = "core"
[[sources]]
id = "arm"
type = "local-file"
target = "server-arm.bin"
platforms = ["linux/arm64"]
[sources.options]
path = "core"
[runtime]
image = "{PIN}"
command = ["server"]
''',
        encoding="utf-8",
    )
    manifest = load_manifest(recipe)
    service = LockService(
        ContentCache(tmp_path / "cache"),
        image_resolver=OciImageResolver(Engine(engine)),
    )
    lock = service.create(manifest, tmp_path)
    assert lock.target == target
    assert list(lock.sources) == ["amd" if target.endswith("amd64") else "arm"]
    assert parse_lockfile(lock.to_bytes()) == lock
    incompatible = replace(
        manifest, package=replace(manifest.package, platforms=("linux/other",))
    )
    with pytest.raises(ResolutionError, match="recipe does not support target"):
        service.create(incompatible, tmp_path)
    monkeypatch.setattr(
        "luminesk_cli.cli.commands.common.container_platform", lambda: target
    )
    assert validate_frozen_lock(lock, manifest, service.cache) == lock
    wrong = replace(lock, target="windows/amd64")
    with pytest.raises(ValidationError, match="frozen lock target"):
        validate_frozen_lock(wrong, manifest, service.cache)
    assert wrong.target == "windows/amd64"
    path = tmp_path / "luminesk.lock"
    write_lockfile(path, wrong)
    original = path.read_bytes()
    with pytest.raises(ValidationError, match="existing lock target"):
        resolve_lock(tmp_path, manifest, frozen=False)
    assert path.read_bytes() == original


@pytest.mark.parametrize("target", ["windows/amd64", "linux/386", "", "invalid"])
def test_engine_rejects_unsupported_platform(target: str) -> None:
    with pytest.raises(ResolutionError, match="Linux containers"):
        OciImageResolver(Engine(target)).target_platform()


def test_pinned_image_is_explicitly_pulled_with_real_safe_progress() -> None:
    engine = Engine()
    events: list[ProgressEvent] = []
    with observe_progress(events.append):
        OciImageResolver(engine).ensure_available(PIN, "linux/amd64")
    assert [call[1] for call in engine.calls] == ["info", "image", "pull", "image"]
    assert engine.calls[2][-1] == PIN
    assert ProgressEvent("image-download") in events
    assert ProgressEvent("image-extract") in events
    assert ProgressEvent("image", completed=2, unit="layers") in events
    assert "secret" not in repr(events)
    assert all(event.total is None for event in events)
    engine.calls.clear()
    OciImageResolver(engine).ensure_available(PIN, "linux/amd64")
    assert [call[1] for call in engine.calls] == ["info", "image"]


def test_lock_architecture_is_not_silently_changed() -> None:
    engine = Engine("linux/arm64")
    with pytest.raises(ResolutionError, match="locked target linux/amd64"):
        OciImageResolver(engine).ensure_available(PIN, "linux/amd64")
    assert [call[1] for call in engine.calls] == ["info"]


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ("cannot connect to docker daemon pipe", "engine unavailable"),
        ("lookup registry: no such host", "DNS lookup"),
        ("unauthorized: token=secret", "access denied"),
        ("no matching manifest for linux/arm64", "OS/architecture"),
        ("TLS certificate unknown", "TLS/proxy"),
    ],
)
def test_inspect_errors_are_not_mistaken_for_missing_images(
    diagnostic: str,
    expected: str,
) -> None:
    calls: list[str] = []

    def runner(
        argv: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv[1])
        return subprocess.CompletedProcess(
            argv, 1, "", diagnostic + " https://user:secret@example.org"
        )

    with pytest.raises(ResolutionError, match=expected) as raised:
        OciImageResolver(runner).resolve("example/server:latest", target="linux/amd64")
    assert calls == ["image"]
    assert "secret" not in str(raised.value)
    assert "secret" not in repr(raised.value.details)


@pytest.mark.parametrize("operation", ["info", "image", "pull"])
def test_oci_timeout_has_operation_without_raw_command(operation: str) -> None:
    engine = Engine()

    def runner(
        argv: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[1] == operation:
            raise subprocess.TimeoutExpired(argv, 30, stderr="secret")
        return engine(argv, **kwargs)

    with pytest.raises(
        ResolutionError, match=f"Docker {operation} timed out"
    ) as raised:
        OciImageResolver(runner).ensure_available(PIN, "linux/amd64")
    assert "secret" not in str(raised.value)


def test_streaming_process_reports_before_exit_and_interrupts() -> None:
    lines: list[str] = []

    def receive(line: str) -> None:
        lines.append(line)
        raise KeyboardInterrupt

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_docker(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('progress'); time.sleep(30)",
            ],
            timeout=10,
            on_line=receive,
        )
    assert lines == ["progress"]
    assert time.monotonic() - started < 10


def test_silent_process_timeout_is_bounded() -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_docker([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
    assert time.monotonic() - started < 10


def test_streaming_capture_is_bounded() -> None:
    result = run_docker(
        [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write('x' * {MAX_OUTPUT + 100})",
        ],
        timeout=10,
    )
    assert len(result.stdout) == MAX_OUTPUT
    assert result.returncode == 0


def test_engine_stderr_does_not_corrupt_metadata_or_container_id() -> None:
    lines: list[str] = []
    result = run_docker(
        [
            sys.executable,
            "-c",
            "import sys; print('container-id'); print('warning', file=sys.stderr)",
        ],
        timeout=10,
        on_line=lines.append,
    )
    assert result.stdout.strip() == "container-id"
    assert result.stderr.strip() == "warning"
    assert sorted(lines) == ["container-id", "warning"]


def test_default_resolver_uses_streaming_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        assert kwargs["timeout"] == ENGINE_TIMEOUT
        return subprocess.CompletedProcess(argv, 0, "linux/aarch64", "")

    monkeypatch.setattr(oci, "run_docker", run)
    assert OciImageResolver().target_platform() == "linux/arm64"
    assert calls[0][1] == "info"


def test_pull_bytes_are_engine_counts_not_estimated_percentages() -> None:
    events: list[ProgressEvent] = []
    with observe_progress(events.append):
        reporter = oci._PullProgress()
        reporter("aabbccddeeff: Downloading [====> ]  1.5MB/3MB")
        reporter("aabbccddeeff: Extracting [=> ]  500kB/3MB")
        reporter("user:secret@example.org: Downloading 1MB/3MB")
    assert events == [
        ProgressEvent("image-download", completed=1_500_000, total=3_000_000),
        ProgressEvent("image-extract", completed=500_000, total=3_000_000),
    ]
