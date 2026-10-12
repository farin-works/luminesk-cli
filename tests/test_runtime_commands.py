from __future__ import annotations

import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from luminesk_cli.application.runtime import DockerRuntime
from luminesk_cli.cli import attach_tui
from luminesk_cli.cli.commands import runtime as runtime_commands
from luminesk_cli.cli.entry import main
from luminesk_cli.domain.errors import RuntimeOperationError, ValidationError


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.logs_result: int | str = "server output"
        self.running = SimpleNamespace(
            runtime=SimpleNamespace(status="running", container_id="container-id"),
            last_readiness_at="2026-08-31T00:00:00+00:00",
            tag="fixture",
        )
        self.stopped = SimpleNamespace(
            runtime=SimpleNamespace(status="stopped", container_id=None),
            last_readiness_at=None,
            tag="fixture",
        )

    def start(self, root: Path, **kwargs: Any) -> Any:
        self.calls.append(("start", (root, kwargs)))
        return self.running

    def stop(self, root: Path) -> Any:
        self.calls.append(("stop", root))
        return self.stopped

    def status(self, root: Path) -> Any:
        self.calls.append(("status", root))
        return self.running

    def logs(
        self,
        root: Path,
        *,
        follow: bool,
        tail: int,
        since: str | None,
        timestamps: bool,
    ) -> int | str:
        self.calls.append(("logs", (root, follow, tail, since, timestamps)))
        return self.logs_result


def _namespace(root: Path, **overrides: Any) -> Namespace:
    values = {
        "dir": str(root),
        "set": ["port=19133"],
        "set_file": [],
        "no_wait": True,
        "attach": False,
        "json": False,
        "follow": False,
        "tail": 200,
        "since": None,
        "timestamps": False,
        "non_interactive": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_runtime_command_handlers_delegate_and_emit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "instance"
    root.mkdir()
    (root / "luminesk.toml").write_text("fixture", encoding="utf-8")
    fake = FakeRuntime()
    emitted: list[tuple[dict[str, Any], str, str]] = []
    manifest = object()

    monkeypatch.setattr(runtime_commands, "DockerRuntime", lambda: fake)
    monkeypatch.setattr(runtime_commands, "recipe", lambda path: (path, manifest))
    monkeypatch.setattr(
        runtime_commands,
        "parse_inputs",
        lambda loaded_manifest, values, file_values: {"port": 19133},
    )
    monkeypatch.setattr(
        runtime_commands,
        "emit",
        lambda namespace, payload, plain, *, tone="success": emitted.append(
            (payload, plain, tone)
        ),
    )

    def run_tui(attached_root: Path, attached_runtime: DockerRuntime) -> int:
        fake.calls.append(("attach", (attached_root, attached_runtime)))
        return 0

    monkeypatch.setattr(
        attach_tui,
        "run_attach_tui",
        run_tui,
    )
    namespace = _namespace(root)

    assert runtime_commands.start(namespace) == 0
    assert runtime_commands.stop(namespace) == 0
    assert runtime_commands.restart(namespace) == 0
    assert runtime_commands.status(namespace) == 0
    assert runtime_commands.logs(namespace) == 0
    assert runtime_commands.attach(namespace) == 0
    assert (
        runtime_commands.logs(_namespace(root, tail=25, since="10m", timestamps=True))
        == 0
    )

    fake.logs_result = 7
    with pytest.raises(RuntimeOperationError, match="stream"):
        runtime_commands.logs(namespace)
    assert fake.calls[0] == (
        "start",
        (
            root.resolve(),
            {"input_overrides": {"port": 19133}, "wait_for_readiness": False},
        ),
    )
    assert [call[0] for call in fake.calls].count("stop") == 2
    assert ("attach", (root.resolve(), fake)) in fake.calls
    assert (
        "logs",
        (root.resolve(), False, 25, "10m", True),
    ) in fake.calls
    assert emitted[-1] == ({"logs": "server output"}, "server output", "plain")


def test_runtime_command_rejects_noninteractive_streams(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="--json"):
        runtime_commands.logs(_namespace(tmp_path, json=True, follow=True))

    with pytest.raises(ValidationError, match="interactive"):
        runtime_commands.attach(_namespace(tmp_path, json=True))

    with pytest.raises(ValidationError, match="interactive"):
        runtime_commands.attach(_namespace(tmp_path, non_interactive=True))


@pytest.mark.parametrize("flag", ["--json", "--non-interactive"])
def test_start_attach_rejects_automation_before_instance_or_docker_access(
    flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden() -> None:
        pytest.fail("Docker must not be accessed for invalid attach arguments")

    monkeypatch.setattr(runtime_commands, "DockerRuntime", forbidden)
    assert main(["start", "--attach", flag]) == 3
    output = capsys.readouterr()
    if flag == "--json":
        payload = json.loads(output.out)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "validation"
        assert "--attach" in payload["error"]["message"]
    else:
        assert "--attach" in output.err


@pytest.mark.parametrize(
    "stdin_tty,stdout_tty", [(False, True), (True, False), (False, False)]
)
def test_start_attach_requires_both_ttys_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdin_tty: bool, stdout_tty: bool
) -> None:
    monkeypatch.setattr(runtime_commands.sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(runtime_commands.sys.stdout, "isatty", lambda: stdout_tty)

    def forbidden() -> None:
        pytest.fail("Docker must not be accessed without a TTY")

    monkeypatch.setattr(runtime_commands, "DockerRuntime", forbidden)
    with pytest.raises(ValidationError, match="interactive TTY"):
        runtime_commands.start(_namespace(tmp_path, attach=True))


def test_plain_start_keeps_tui_imports_lazy() -> None:
    script = """
import sys
from pathlib import Path
from types import SimpleNamespace
from luminesk_cli.cli.commands import runtime
from luminesk_cli.cli.entry import main
state = SimpleNamespace(
    runtime=SimpleNamespace(status='running', container_id='fixture'),
    last_readiness_at=None, tag='fixture',
)
runtime._instance_root = lambda value: Path('.')
runtime.recipe = lambda root: (root, None)
runtime.parse_inputs = lambda *args: {}
runtime.DockerRuntime.start = lambda *args, **kwargs: state
assert main(['start', '--json']) == 0
assert 'prompt_toolkit' not in sys.modules
assert 'luminesk_cli.cli.attach_tui' not in sys.modules
assert 'luminesk_cli.cli.console_logs' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_instance_root_discovers_parent_and_validates_explicit_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "instance"
    child = root / "worlds" / "example"
    child.mkdir(parents=True)
    (root / "luminesk.toml").write_text("fixture", encoding="utf-8")
    monkeypatch.chdir(child)

    assert runtime_commands._instance_root(None) == root.resolve()
    assert runtime_commands._instance_root(str(root)) == root.resolve()

    with pytest.raises(ValidationError, match="is not a Luminesk instance"):
        runtime_commands._instance_root(str(tmp_path / "missing"))

    (root / "luminesk.toml").unlink()

    with pytest.raises(ValidationError, match="no luminesk.toml found"):
        runtime_commands._instance_root(None)
