from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from luminesk_cli.cli.entry import main
from luminesk_cli.domain.lockfile import Lockfile, RecipeLock, RuntimeLock
from luminesk_cli.domain.manifest import parse_manifest
from luminesk_cli.domain.plan import Plan
from luminesk_cli.domain.preview import Preview
from luminesk_cli.infrastructure.recipe_snapshot import create_recipe_snapshot
from luminesk_cli.infrastructure.state import load_state

pytestmark = pytest.mark.usefixtures("docker_engine")


def test_human_output_uses_brand_semantic_colors() -> None:
    from luminesk_cli.cli.output import THEME, human_message

    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        highlight=False,
        theme=THEME,
        width=120,
    )
    message = human_message(
        "Install preview\n"
        "  Runtime image: example/server@sha256:" + "a" * 64 + "\n"
        "  add      server.jar — managed file\n"
        "Warning: review this change",
        tone="info",
    )

    console.print(message)
    rendered = stream.getvalue()

    assert "\x1b[" in rendered
    assert "38;2;255;51;121m" in rendered
    assert "38;2;0;210;106m" in rendered
    assert "38;2;255;200;61m" in rendered
    assert message.plain == (
        "Install preview\n"
        "  Runtime image: example/server@sha256:" + "a" * 64 + "\n"
        "  add      server.jar — managed file\n"
        "Warning: review this change"
    )


def test_human_output_does_not_interpret_markup_or_terminal_controls() -> None:
    from luminesk_cli.cli.output import human_message

    message = human_message("Created [red]server[/red]\x1b[31m")

    assert message.plain == "Created [red]server[/red]�[31m"


def test_version_cold_path_does_not_import_heavy_dependencies() -> None:
    script = (
        "import sys; from luminesk_cli.cli.entry import main; "
        "code=main(['--version']); "
        "print(','.join(n for n in ('httpx','rich','sqlite3','cyclopts') "
        "if n in sys.modules)); raise SystemExit(code)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )

    assert result.returncode == 0
    assert result.stdout == "Luminesk 2.0.2\n\n"


def test_doctor_reports_healthy_cli_and_daemon(monkeypatch, capsys) -> None:
    from luminesk_cli.cli.commands import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda executable: "/usr/bin/docker")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, '{"Client":{"Version":"28.0"}}\n', ""
        ),
    )
    assert main(["doctor", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)

    assert [item["component"] for item in payload["checks"]] == ["docker"]
    assert payload["checks"][0]["daemonReachable"] is True


@pytest.mark.parametrize("available", [False, True])
def test_json_stays_clean_on_a_color_terminal(
    available: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from luminesk_cli.cli.commands import doctor

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(
        doctor.shutil, "which", lambda executable: "/docker" if available else None
    )
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "{}", ""),
    )

    assert main(["doctor", "--json", "--non-interactive"]) == (0 if available else 8)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "\x1b" not in captured.out
    assert json.loads(captured.out)["ok"] is available


def test_debug_diagnostics_use_stderr_without_changing_json(
    monkeypatch, capsys
) -> None:
    from luminesk_cli.cli.commands import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda executable: "/usr/bin/docker")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, '{"Client":{"Version":"27"}}', ""
        ),
    )

    assert main(["doctor", "--debug", "--json"]) == 0
    captured = capsys.readouterr()

    assert json.loads(captured.out)["ok"] is True
    assert "DEBUG luminesk_cli.cli.dispatch: command started name=doctor" in (
        captured.err
    )
    assert "command completed name=doctor exit_code=0" in captured.err

    assert main(["doctor", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert captured.err == ""


def test_debug_trace_omits_exception_text(capsys) -> None:
    import logging

    from luminesk_cli.cli.debug import configure_debug, log_exception_frames

    secret = "never-log-this-secret"
    configure_debug(True)
    try:
        try:
            raise RuntimeError(secret)
        except RuntimeError as exc:
            log_exception_frames(logging.getLogger("luminesk_cli.security-test"), exc)
    finally:
        configure_debug(False)

    captured = capsys.readouterr()
    assert "exception_type=RuntimeError" in captured.err
    assert secret not in captured.err


def test_doctor_fails_when_docker_is_unavailable(monkeypatch, capsys) -> None:
    from luminesk_cli.cli.commands import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda executable: None)

    assert main(["doctor", "--json"]) == 8
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "runtime"
    assert payload["error"]["details"]["checks"][0]["available"] is False


def test_human_runtime_error_includes_sanitized_docker_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from luminesk_cli.cli.commands import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda executable: "/usr/bin/docker")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            "",
            "permission denied\x1b[31m\n",
        ),
    )

    assert main(["doctor"]) == 8
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "Docker daemon is not reachable" in captured.err
    assert "permission denied�[31m" in captured.err
    assert "\x1b" not in captured.err


def test_human_error_does_not_expose_unapproved_detail_fields(capsys) -> None:
    from luminesk_cli.cli.output import print_error

    print_error("security", "request rejected", details={"stderr": "secret-value"})

    assert "secret-value" not in capsys.readouterr().err


def test_human_errors_are_written_to_stderr(tmp_path: Path, capsys) -> None:
    assert main(["validate", "--dir", str(tmp_path)]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error [validation]" in captured.err


def test_parser_usage_errors_honor_json(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["search", "--unknown-option", "--json"])

    assert raised.value.code == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["error"]["code"] == "usage"
    assert "usage:" in payload["error"]["details"]["usage"]


def test_local_cli_install_emits_json_and_writes_instance(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    root = tmp_path / "server"
    root.mkdir()
    (root / "server.jar.in").write_bytes(b"server")
    template = root / "template"
    template.mkdir()
    (template / "runtime.txt.tmpl").write_text(
        "memory=${input.memory}\n", encoding="utf-8"
    )
    (root / "luminesk.toml").write_text(
        """\
manifest_version = 1
template = "template"
[package]
name = "cli-fixture"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "bedrock"
[inputs.memory]
type = "string"
default = "2g"
[inputs.port]
type = "integer"
default = 25565
[inputs.data_dir]
type = "string"
default = "data"
[[sources]]
id = "core"
type = "local-file"
target = "server.jar"
[sources.options]
path = "server.jar.in"
[runtime]
image = "fixture/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["java", "-Xmx${input.memory}", "-jar", "server.jar"]
memory = "${input.memory}"
[[runtime.mounts]]
source = "${input.data_dir}"
target = "/server/${input.data_dir}"
[[runtime.ports]]
name = "game"
host = "${input.port}"
container = "${input.port}"
""",
        encoding="utf-8",
    )

    exit_code = main(["install", "--dir", str(root), "--set", "memory=3g", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["dryRun"] is False
    assert payload["preview"]["trust"]["classification"] == "local"
    assert payload["preview"]["capabilities"]["runtime"]["image"].startswith(
        "fixture/server@sha256:"
    )
    assert payload["preview"]["capabilities"]["runtime"]["command"] == [
        "java",
        "-Xmx3g",
        "-jar",
        "server.jar",
    ]
    assert payload["preview"]["capabilities"]["runtime"]["memory"] == "3g"
    assert payload["preview"]["capabilities"]["runtime"]["mounts"] == [
        {"source": "data", "target": "/server/data", "mode": "rw"}
    ]
    assert payload["preview"]["capabilities"]["runtime"]["ports"] == [
        {
            "name": "game",
            "host": 25565,
            "container": 25565,
            "protocol": "tcp",
        }
    ]
    assert payload["preview"]["plan"]["changes"] == payload["changes"]
    assert (root / "server.jar").read_bytes() == b"server"
    assert (root / "runtime.txt").read_text(encoding="utf-8") == "memory=3g\n"
    assert (root / ".luminesk_cli/state.json").is_file()
    lock = json.loads((root / "luminesk.lock").read_text(encoding="utf-8"))
    assert lock["recipe"] == {
        "kind": "local",
        "source": "local",
        "revision": lock["manifestDigest"],
        "entry": None,
        "path": None,
        "ref": None,
        "tracking": False,
        "version": "2.0.0",
        "manifestDigest": lock["manifestDigest"],
        "templateDigest": payload["preview"]["trust"]["templateDigest"],
    }

    from luminesk_cli.cli.commands import update as update_command

    def reject_upstream_fetch(*args, **kwargs):
        del args, kwargs
        raise AssertionError("offline diff fetched the upstream recipe")

    with monkeypatch.context() as offline_patch:
        offline_patch.setattr(
            update_command,
            "_candidate_recipe",
            reject_upstream_fetch,
        )
        assert main(["diff", "--dir", str(root), "--offline", "--json"]) == 0
    offline_diff = json.loads(capsys.readouterr().out)
    assert offline_diff["recipeDrift"] == []
    assert offline_diff["upstreamRecipeDiff"] is None
    assert offline_diff["upstreamChecked"] is False
    assert offline_diff["managedFileDrift"] == []

    assert main(["diff", "--dir", str(root), "--json"]) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["recipeDrift"] == []
    assert diff["upstreamRecipeDiff"] == []
    assert diff["upstreamChecked"] is True
    assert diff["managedFileDrift"] == []

    assert main(["plan", "--dir", str(root), "--frozen", "--json"]) == 0
    persisted_plan = json.loads(capsys.readouterr().out)
    assert persisted_plan["ok"] is True
    assert all(
        change["action"] == "preserve" for change in persisted_plan["plan"]["changes"]
    )

    with (root / "luminesk.toml").open("a", encoding="utf-8") as handle:
        handle.write("\n# local drift\n")
    assert main(["start", "--dir", str(root), "--json"]) != 0
    error = json.loads(capsys.readouterr().out)
    assert "differs from the locked recipe snapshot" in error["error"]["message"]


def test_interactive_install_wizard_collects_and_validates_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "wizard-server"
    template = root / "template"
    template.mkdir(parents=True)
    (template / "eula.txt.tmpl").write_text("eula=${input.eula}\n", encoding="utf-8")
    (template / "server.properties.tmpl").write_text(
        "motd=${input.server_name}\n", encoding="utf-8"
    )
    (root / "luminesk.toml").write_text(
        """\
manifest_version = 1
template = "template"
[package]
name = "wizard-fixture"
version = "2.0.0"
display_name = "Wizard Fixture"
kind = "core"
game = "minecraft"
edition = "java"
[inputs.eula]
type = "boolean"
required = true
prompt = "Accept the Minecraft EULA"
[inputs.server_name]
type = "string"
default = "Default Server"
pattern = "^[A-Za-z ]+$"
prompt = "Server name"
[runtime]
image = "example/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["server"]
""",
        encoding="utf-8",
    )
    answers = iter(["maybe", "yes", "Custom Server"])
    monkeypatch.setattr("builtins.input", lambda: next(answers))

    assert main(["install", "--dir", str(root)]) == 0

    captured = capsys.readouterr()
    assert "Configure inputs for Wizard Fixture" in captured.out
    assert "Accept the Minecraft EULA" in captured.out
    assert "Server name" in captured.out
    assert "must be true/false or yes/no" in captured.err
    assert (root / "eula.txt").read_text(encoding="utf-8") == "eula=true\n"
    assert (root / "server.properties").read_text(encoding="utf-8") == (
        "motd=Custom Server\n"
    )
    state = load_state(root)
    assert state is not None
    assert state.inputs == {"eula": True, "server_name": "Custom Server"}


def test_non_interactive_install_reports_every_missing_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "missing-inputs"
    template = root / "template"
    template.mkdir(parents=True)
    (template / "config.txt.tmpl").write_text(
        "${input.eula}:${input.token}\n", encoding="utf-8"
    )
    (root / "luminesk.toml").write_text(
        """\
manifest_version = 1
template = "template"
[package]
name = "missing-input-fixture"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "java"
[inputs.eula]
type = "boolean"
required = true
[inputs.token]
type = "string"
required = true
secret = true
[runtime]
image = "example/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["server"]
""",
        encoding="utf-8",
    )

    assert main(["install", "--dir", str(root), "--json"]) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["message"] == (
        "required inputs have no values: eula, token"
    )
    assert payload["error"]["details"]["missingInputs"] == [
        {"name": "eula", "option": "--set"},
        {"name": "token", "option": "--set-file"},
    ]


def test_validate_build_accepts_value_and_secret_file_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "validate-inputs"
    template = root / "template"
    template.mkdir(parents=True)
    (template / "config.txt.tmpl").write_text(
        "${input.eula}:${input.token}\n", encoding="utf-8"
    )
    secret = tmp_path / "token"
    secret.write_text("secret\n", encoding="utf-8")
    (root / "luminesk.toml").write_text(
        """\
manifest_version = 1
template = "template"
[package]
name = "validate-input-fixture"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "java"
[inputs.eula]
type = "boolean"
required = true
[inputs.token]
type = "string"
required = true
secret = true
[runtime]
image = "example/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["server"]
""",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "validate",
                "--dir",
                str(root),
                "--build",
                "--set",
                "eula=true",
                "--set-file",
                f"token={secret}",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert [item["phase"] for item in payload["validation"]] == [
        "static",
        "resolve",
        "build",
    ]


def test_validate_rejects_bad_input_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from luminesk_cli.cli.commands import validate as validate_command

    root = tmp_path / "validate-bad-input"
    root.mkdir()
    (root / "payload.in").write_text("payload", encoding="utf-8")
    (root / "luminesk.toml").write_text(
        """\
manifest_version = 1
[package]
name = "validate-bad-input-fixture"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "java"
[inputs.port]
type = "integer"
min = 1
max = 65535
[[files]]
source = "payload.in"
target = "payload"
[runtime]
image = "example/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["server"]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        validate_command,
        "resolve_lock",
        lambda *args, **kwargs: pytest.fail("resolution must not run"),
    )

    assert (
        main(
            [
                "validate",
                "--dir",
                str(root),
                "--build",
                "--set",
                "port=70000",
                "--json",
            ]
        )
        == 3
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["message"] == "input port is above its maximum"


def test_update_wizard_reprompts_only_for_unpersisted_secret_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "secret-update"
    template = root / "template"
    template.mkdir(parents=True)
    (template / "credentials.txt.tmpl").write_text("${input.token}\n", encoding="utf-8")
    secret = tmp_path / "token"
    secret.write_text("secret value\n", encoding="utf-8")
    (root / "luminesk.toml").write_text(
        """\
manifest_version = 1
template = "template"
[package]
name = "secret-update-fixture"
version = "2.0.0"
display_name = "Secret Update Fixture"
kind = "core"
game = "minecraft"
edition = "java"
[inputs.server_name]
type = "string"
default = "Saved Server"
prompt = "Server name"
[inputs.token]
type = "string"
required = true
secret = true
prompt = "Provider token"
[runtime]
image = "example/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
command = ["server"]
""",
        encoding="utf-8",
    )
    assert (
        main(
            [
                "install",
                "--dir",
                str(root),
                "--set-file",
                f"token={secret}",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda: str(secret))

    assert main(["update", "--dir", str(root), "--dry-run"]) == 0

    captured = capsys.readouterr()
    assert "Configure inputs for Secret Update Fixture" in captured.out
    assert "Provider token" in captured.out
    assert "Path for token:" in captured.out
    assert "Server name" not in captured.out
    assert "secret value" not in captured.out


def test_remote_recipe_dry_run_is_built_and_planned_without_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    from luminesk_cli.cli.commands import install as install_command

    manifest_bytes = b"""\
manifest_version = 1
[package]
name = "remote-fixture"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "bedrock"
[[sources]]
id = "artifact"
type = "local-file"
target = "server.bin"
[sources.options]
path = "artifact.bin"
[runtime]
image = "example/server:2"
command = ["server"]
"""
    manifest = parse_manifest(manifest_bytes)
    lockfile = Lockfile(
        manifest_digest=manifest.digest,
        target="linux/amd64",
        sources={},
        runtime=RuntimeLock(image=f"example/server@sha256:{'a' * 64}"),
        recipe=RecipeLock(
            kind="github",
            source="github:owner/repo",
            revision="b" * 40,
            version="2.0.0",
            manifest_digest=manifest.digest,
            ref="main",
            tracking=True,
        ),
    )
    root = tmp_path / "recipe"
    root.mkdir()
    (root / "luminesk.toml").write_bytes(manifest_bytes)
    (root / "artifact.bin").write_bytes(b"artifact")
    snapshot = create_recipe_snapshot(
        root,
        manifest,
        kind="github",
        source="github:owner/repo",
        revision="b" * 40,
        ref="main",
        tracking=True,
    )
    preview = Preview.for_install(snapshot, lockfile, Plan("install", "target", ()))
    rendered_preview = preview.to_text()
    assert "    . -> /server (rw)" in rendered_preview
    for section in (
        "Trust:",
        "Manifest digest:",
        "Resolved artifacts:",
        "Runtime command:",
        "Mounts:",
        "Ports:",
        "Ownership preserve:",
        "Checks:",
        "Changes (0):",
    ):
        assert section in rendered_preview
    events: list[str] = []
    monkeypatch.setattr(
        install_command,
        "resolve_lock",
        lambda *args, **kwargs: lockfile,
    )
    monkeypatch.setattr(
        install_command,
        "_confirm",
        lambda *args: events.append("confirm"),
    )

    def build(*args):
        events.append("build")
        package = SimpleNamespace(metadata=SimpleNamespace(files=()))
        return SimpleNamespace(cleanup=lambda: None), package

    class FakeInstaller:
        def __init__(self, **kwargs):
            del kwargs

        def plan(self, package, target):
            del package
            events.append("plan")
            return Plan("install", str(target), ())

    monkeypatch.setattr(install_command, "build_package", build)
    monkeypatch.setattr(install_command, "TransactionalInstaller", FakeInstaller)
    namespace = SimpleNamespace(
        frozen=False,
        set=[],
        set_file=[],
        dry_run=True,
        json=False,
        yes=False,
        non_interactive=True,
    )

    assert (
        install_command._install_snapshot(
            namespace,
            snapshot,
            tmp_path / "target",
            confirm=True,
        )
        == 0
    )

    assert events == ["build", "plan"]


@pytest.mark.parametrize("option", ["--set", "--set-file"])
def test_plan_validates_input_syntax_before_resolution(
    option: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from luminesk_cli.cli.commands import plan as plan_command

    root = tmp_path / "recipe"
    root.mkdir()
    (root / "luminesk.toml").write_text(
        """\
manifest_version = 1
[package]
name = "plan-input-fixture"
version = "2.0.0"
kind = "core"
game = "minecraft"
edition = "java"
[inputs.memory]
type = "string"
[[sources]]
id = "server"
type = "http"
target = "server.jar"
[sources.options]
url = "https://example.invalid/server.jar"
[runtime]
image = "example/server:latest"
command = ["java", "-jar", "server.jar"]
""",
        encoding="utf-8",
    )

    def reject_resolution(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("resolution ran before input validation")

    monkeypatch.setattr(plan_command, "resolve_lock", reject_resolution)

    assert (
        main(
            [
                "plan",
                "--dir",
                str(root),
                option,
                "malformed-input",
                "--json",
            ]
        )
        == 3
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "validation"
    assert "must be KEY=VALUE" in payload["error"]["message"]
