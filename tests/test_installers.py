from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

BUNDLE_ASSETS = {
    "luminesk-linux-amd64.zip",
    "luminesk-linux-arm64.zip",
    "luminesk-macos-amd64.zip",
    "luminesk-macos-arm64.zip",
    "luminesk-windows-amd64.zip",
}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def archive_bundle(source: Path, output: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts/archive_bundle.py"),
            str(source),
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr)


def test_release_matrix_matches_installer_assets() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/release-pypi.yml").read_text(
        encoding="utf-8"
    )
    for name in BUNDLE_ASSETS:
        assert (
            f"name: {name.removeprefix('luminesk-').removesuffix('.zip')}" in workflow
        )
    assert "--onefile" not in workflow
    assert "--collect-all prompt_toolkit" in workflow
    assert "--runtime-hook scripts/bundle_runtime_hook.py" in workflow


def test_wheel_verifier_parses_dependency_requirements(tmp_path: Path) -> None:
    wheel = tmp_path / "luminesk_cli-2.1.0-py3-none-any.whl"
    metadata = """\
Metadata-Version: 2.4
Name: luminesk-cli
Version: 2.1.0
Requires-Dist: rich>=15.0.0
Requires-Dist: platformdirs[tests] >= 4.10.0; python_version >= "3.13"
"""
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("luminesk_cli/__init__.py", "")
        archive.writestr("luminesk_cli-2.1.0.dist-info/METADATA", metadata)
    command = [
        sys.executable,
        str(REPOSITORY_ROOT / "scripts/verify_wheel.py"),
        str(wheel),
    ]

    accepted = subprocess.run(command, check=False, capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr

    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("luminesk_cli/__init__.py", "")
        archive.writestr(
            "luminesk_cli-2.1.0.dist-info/METADATA",
            metadata + "Requires-Dist: cyclopts>=3.0\n",
        )
    rejected = subprocess.run(command, check=False, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "cyclopts" in rejected.stderr


def test_release_asset_verifier_accepts_only_complete_hashed_set(
    tmp_path: Path,
) -> None:
    names = BUNDLE_ASSETS | {
        "luminesk-cli.cdx.json",
        "luminesk_cli-2.0.2-py3-none-any.whl",
        "luminesk_cli-2.0.2.tar.gz",
    }
    for name in names:
        if name in BUNDLE_ASSETS:
            with zipfile.ZipFile(tmp_path / name, "w") as archive:
                executable = "nesk.exe" if "windows" in name else "nesk"
                for member in (
                    f"nesk/{executable}",
                    "nesk/_internal/",
                    "nesk/.bundle-manifest",
                ):
                    info = zipfile.ZipInfo(member)
                    info.create_system = 3
                    info.external_attr = (
                        (stat.S_IFDIR if member.endswith("/") else stat.S_IFREG) | 0o755
                    ) << 16
                    archive.writestr(info, b"" if member.endswith("/") else b"fixture")
        else:
            (tmp_path / name).write_bytes(f"fixture:{name}".encode())
    sums = "".join(
        f"{hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(names)
    )
    (tmp_path / "SHA256SUMS").write_text(sums, encoding="ascii")
    command = [
        sys.executable,
        str(REPOSITORY_ROOT / "scripts/verify_release_assets.py"),
        str(tmp_path),
    ]

    valid = subprocess.run(command, check=False, capture_output=True, text=True)
    assert valid.returncode == 0, valid.stderr

    unsupported = tmp_path / "luminesk-windows-arm64.zip"
    unsupported.write_bytes(b"unsupported bundle")
    unexpected = subprocess.run(command, check=False, capture_output=True, text=True)
    assert unexpected.returncode != 0
    assert "unexpected release assets: luminesk-windows-arm64.zip" in unexpected.stderr
    unsupported.unlink()

    (tmp_path / "luminesk-linux-amd64.zip").unlink()
    invalid = subprocess.run(command, check=False, capture_output=True, text=True)
    assert invalid.returncode != 0
    assert "missing" in invalid.stderr


def test_bundle_archive_is_deterministic_and_preserves_modes(tmp_path: Path) -> None:
    source = tmp_path / "nesk"
    (source / "_internal").mkdir(parents=True)
    (source / "nesk").write_bytes(b"executable")
    (source / "nesk").chmod(0o755)
    (source / "_internal/library").write_bytes(b"library")
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    archive_bundle(source, first)
    archive_bundle(source, second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert stat.S_ISREG(archive.getinfo("nesk/nesk").external_attr >> 16)
        if os.name != "nt":
            assert archive.getinfo("nesk/nesk").external_attr >> 16 & 0o111
            assert b"F 755" in archive.read("nesk/.bundle-manifest")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and mode contract")
def test_bundle_preserves_internal_links_and_rejects_escape(tmp_path: Path) -> None:
    source = tmp_path / "nesk"
    (source / "_internal/real").mkdir(parents=True)
    (source / "nesk").write_bytes(b"executable")
    (source / "_internal/real/lib").write_bytes(b"library")
    (source / "_internal/link").symlink_to("real/lib")
    (source / "_internal/directory").symlink_to("real", target_is_directory=True)
    archive_bundle(source, tmp_path / "bundle.zip")
    with zipfile.ZipFile(tmp_path / "bundle.zip") as archive:
        member = archive.getinfo("nesk/_internal/link")
        assert stat.S_ISLNK(member.external_attr >> 16)
        assert archive.read(member) == b"real/lib"
        assert "nesk/_internal/directory/lib" not in archive.namelist()
    (source / "_internal/escape").symlink_to("../..")
    with pytest.raises(ValueError, match="escapes"):
        archive_bundle(source, tmp_path / "invalid.zip")


@pytest.fixture(scope="module")
def windows_fixture_exe(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if os.name != "nt":
        pytest.skip("Windows installer integration")
    directory = tmp_path_factory.mktemp("fixture-exe")
    source = directory / "Fixture.cs"
    source.write_text(
        r"""using System;
using System.IO;
class Fixture {
    static int Main(string[] args) {
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        string root = AppDomain.CurrentDomain.BaseDirectory;
        if (File.Exists(Path.Combine(root,"fail"))) return 71;
        if (args.Length > 0 && args[0] == "--version") {
            Console.WriteLine("Luminesk " + File.ReadAllText(Path.Combine(root,"version")));
        } else if (args.Length > 0 && args[0] == "echo") {
            for (int i=1;i<args.Length;i++) Console.WriteLine(args[i]);
            Console.WriteLine(Console.ReadLine());
            Console.Error.WriteLine("stderr preserved");
            return 37;
        }
        return 0;
    }
}""",
        encoding="utf-8",
    )
    compiler = (
        Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    )
    executable = directory / "nesk.exe"
    result = subprocess.run(
        [str(compiler), "/nologo", f"/out:{executable}", str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def corrupt_archive(archive: Path, scenario: str) -> None:
    if scenario == "manifest-traversal":
        with zipfile.ZipFile(archive) as original:
            members = [(entry, original.read(entry)) for entry in original.infolist()]
        with zipfile.ZipFile(archive, "w") as output:
            for entry, payload in members:
                if entry.filename == "nesk/.bundle-manifest":
                    payload += b"L 777 0 ../../outside _internal/escape\n"
                output.writestr(entry, payload)
        return
    names = {
        "traversal": "nesk/../../outside",
        "absolute": "/outside",
        "duplicate": "nesk/nesk.exe",
        "link": "nesk/evil",
        "oversize": "nesk/huge",
    }
    if scenario not in names:
        return
    import struct
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(archive, "a") as output:
            info = zipfile.ZipInfo(names[scenario])
            info.create_system = 3
            info.external_attr = (
                stat.S_IFLNK if scenario == "link" else stat.S_IFREG
            ) << 16
            output.writestr(info, b"../../outside")
    if scenario == "oversize":
        payload = bytearray(archive.read_bytes())
        central = payload.rfind(b"PK\x01\x02")
        struct.pack_into("<I", payload, central + 24, 2147483649)
        archive.write_bytes(payload)


def windows_install(
    directory: Path,
    executable: Path,
    *,
    version: str = "2.0.2",
    scenario: str = "ok",
    delete: bool = False,
    bundle_directory: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    source = directory / "fixture/nesk"
    (source / "_internal").mkdir(parents=True, exist_ok=True)
    if bundle_directory is not None:
        shutil.copytree(bundle_directory, source, dirs_exist_ok=True, symlinks=True)
    else:
        shutil.copyfile(executable, source / "nesk.exe")
    (source / "version").write_text(version)
    failure = source / "fail"
    if scenario == "smoke":
        failure.touch()
    elif failure.exists():
        failure.unlink()
    archive = directory / "bundle.zip"
    archive_bundle(source, archive)
    if scenario == "zip":
        archive.write_bytes(b"not a zip")
    corrupt_archive(archive, scenario)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    url = f"https://github.com/farin-works/luminesk-cli/releases/download/v{version}/luminesk-windows-amd64.zip"
    (directory / "release.json").write_text(
        json.dumps(
            {
                "tag_name": f"v{version}",
                "assets": [
                    {
                        "name": "luminesk-windows-amd64.zip",
                        "digest": "sha256:"
                        + ("0" * 64 if scenario == "sha" else digest),
                        "browser_download_url": url,
                    }
                ],
            }
        )
    )
    installer = (REPOSITORY_ROOT / "assets/install.ps1").read_text()
    # Only host boundaries are redirected; extraction, transactions, metadata,
    # launchers and smoke commands are the production implementation.
    installer = installer.replace("$HOME", "$TestHome")
    installer = "\n".join(
        "$isAdmin = $false" if line.startswith("$isAdmin =") else line
        for line in installer.splitlines()
    )
    installer = installer.replace(
        "function Receive-VerifiedHost(", "function Original-Receive-VerifiedHost("
    )
    installer = installer.replace(
        "[Environment]::SetEnvironmentVariable('Path', \"$bin;$savedPath\", $scope)",
        "$null = $savedPath",
    )
    if scenario == "switch":
        installer = installer.replace(
            "Write-Atomic $launcher $launcherText",
            "throw 'simulated activation failure'",
        )
    if scenario == "interrupt":
        installer = installer.replace(
            "Write-Atomic $launcher $launcherText", "[Environment]::Exit(130)"
        )
    if scenario == "post-switch":
        installer = installer.replace(
            "& $launcher --version", "throw 'simulated post-switch failure'"
        )
    if scenario == "lock":
        installer = installer.replace(
            "$lock = $null; $work = $null",
            "$contender = [IO.File]::Open((Join-Path $root 'install.lock'),'OpenOrCreate','ReadWrite','None')\n$lock = $null; $work = $null",
        )
    if scenario == "architecture":
        installer = installer.replace(
            "$rawArch = $env:PROCESSOR_ARCHITEW6432", "$rawArch = 'unsupported'"
        )
    if scenario == "arm64":
        installer = installer.replace(
            "$rawArch = $env:PROCESSOR_ARCHITEW6432", "$rawArch = 'ARM64'"
        )
    prelude = r"""
$TestHome = $env:LUMINESK_TEST_HOME
# Set the fixture architecture inside the child process, before installer code.
$env:PROCESSOR_ARCHITECTURE = 'AMD64'
$env:PROCESSOR_ARCHITEW6432 = 'AMD64'
function Receive-VerifiedHost($Url, $Destination, $Limit, [switch]$ShowProgress) {
    if ($env:LUMINESK_TEST_SCENARIO -eq 'network') { throw 'simulated network failure' }
    if ($Url -notmatch '^https://(?:api\.github\.com/repos|github\.com)/farin-works/luminesk-cli/releases/') { throw 'unexpected release repository' }
    $name = if ($Url.EndsWith('/latest')) { 'release.json' } else { 'bundle.zip' }
    [IO.File]::Copy((Join-Path $env:LUMINESK_TEST_DATA $name), $Destination)
}
"""
    harness = directory / "install-test.ps1"
    harness.write_text(
        prelude + "\n& {\n" + installer + "\n} --yes" + (" --delete" if delete else ""),
        encoding="utf-8-sig",
    )
    home = directory / "home space Юникод"
    home.mkdir(exist_ok=True)
    environment = dict(
        os.environ,
        LUMINESK_TEST_HOME=str(home),
        LUMINESK_TEST_DATA=str(directory),
        LUMINESK_TEST_SCENARIO=scenario,
    )
    environment["PATH"] = (
        str(home / ".local/bin")
        + os.pathsep
        + str(Path(os.environ["WINDIR"]) / "System32")
    )
    environment.pop("PSMODULEPATH", None)
    shell = shutil.which("powershell") or shutil.which("pwsh")
    assert shell
    return subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        env=environment,
        cwd=directory,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_windows_install_update_reinstall_and_uninstall(
    tmp_path: Path, windows_fixture_exe: Path
) -> None:
    for version, installed in (("2.0.2", None), ("2.0.3", "2.0.2"), ("2.0.3", "2.0.3")):
        result = windows_install(tmp_path, windows_fixture_exe, version=version)
        assert result.returncode == 0, result.stdout + result.stderr
        assert_installation_output(result.stdout, version, installed)
        assert "windows-amd64" in result.stdout
        assert "Docker Desktop" in result.stdout
        assert "Linux containers" in result.stdout
    home = tmp_path / "home space Юникод"
    launcher = home / ".local/bin/nesk.cmd"
    assert launcher.is_file()
    versions = home / ".local/share/luminesk/app/versions"
    assert len(list(versions.iterdir())) == 3
    unrelated = home / ".local/bin/other"
    unrelated.write_text("keep")
    cache = home / ".local/share/luminesk/cache"
    cache.mkdir()
    (cache / "world").write_text("keep")
    result = windows_install(tmp_path, windows_fixture_exe, delete=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Removing installer-owned application files..." in result.stdout
    assert "instances and cache were preserved" in result.stdout
    assert "Next steps" not in result.stdout
    assert not launcher.exists()
    assert unrelated.read_text() == "keep"
    assert (cache / "world").read_text() == "keep"


def test_powershell_download_reports_percentage_or_received_bytes(
    tmp_path: Path,
) -> None:
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is required for download progress output")
    installer = (REPOSITORY_ROOT / "assets/install.ps1").read_text(encoding="utf-8")
    definitions = installer[: installer.index("function Test-Bundle(")]
    harness = tmp_path / "progress.ps1"
    harness.write_text(
        definitions
        + """
function Write-Progress($Id, $Activity, $Status, $PercentComplete, $ErrorAction) {
    [pscustomobject]@{Activity=$Activity; Status=$Status; Percent=$PercentComplete}
}
@(
    Write-DownloadProgress 5242880 10485760
    Write-DownloadProgress 10485760 10485760
    Write-DownloadProgress 5242880 -1
) | ConvertTo-Json -Compress
""",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reports = json.loads(result.stdout)
    assert [report["Percent"] for report in reports] == [50, 100, -1]
    assert all(report["Activity"] == "Downloading Luminesk" for report in reports)
    assert "50%" in reports[0]["Status"]
    assert "MiB received" in reports[2]["Status"]
    assert "%" not in reports[2]["Status"]


def test_windows_failed_updates_keep_working_launcher(
    tmp_path: Path, windows_fixture_exe: Path
) -> None:
    result = windows_install(tmp_path, windows_fixture_exe)
    assert result.returncode == 0, result.stdout + result.stderr
    home = tmp_path / "home space Юникод"
    launcher = home / ".local/bin/nesk.cmd"
    previous = launcher.read_bytes()
    metadata = home / ".local/share/luminesk/.metadata_one_line"
    previous_metadata = metadata.read_bytes()

    # Each rejected update must preserve the same installed baseline. Reinstalling
    # it for every scenario repeats the same expensive setup.
    for scenario in (
        "sha",
        "zip",
        "smoke",
        "network",
        "switch",
        "post-switch",
        "lock",
        "architecture",
        "arm64",
        "traversal",
        "absolute",
        "duplicate",
        "link",
        "oversize",
        "manifest-traversal",
    ):
        result = windows_install(
            tmp_path, windows_fixture_exe, version="2.0.3", scenario=scenario
        )
        assert result.returncode != 0, f"{scenario}: {result.stdout}{result.stderr}"
        assert "Luminesk 2.0.3 installed." not in result.stdout
        assert "Next steps" not in result.stdout
        if scenario == "arm64":
            assert "Unsupported architecture: ARM64" in result.stderr
        assert launcher.read_bytes() == previous, scenario
        assert not list(home.rglob("outside")), scenario
        assert metadata.read_bytes() == previous_metadata, scenario


def test_windows_migrates_owned_onefile(
    tmp_path: Path, windows_fixture_exe: Path
) -> None:
    home = tmp_path / "home space Юникод"
    binary = home / ".local/bin/nesk.exe"
    binary.parent.mkdir(parents=True)
    shutil.copyfile(windows_fixture_exe, binary)
    (binary.parent / "version").write_text("2.0.1")
    metadata = home / ".local/share/luminesk/.metadata_one_line"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        f"installed_via=one-line-script\nbinary={binary}\nversion=2.0.1\n",
        encoding="utf-8",
    )
    result = windows_install(tmp_path, windows_fixture_exe)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not binary.exists()
    assert (binary.parent / "nesk.cmd").exists()
    assert list((metadata.parent / "app/versions").glob("*/legacy-nesk.exe"))


def test_windows_refuses_foreign_binary(
    tmp_path: Path, windows_fixture_exe: Path
) -> None:
    binary = tmp_path / "home space Юникод/.local/bin/nesk.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"foreign uv/pipx executable")
    result = windows_install(tmp_path, windows_fixture_exe)
    assert result.returncode != 0
    assert binary.read_bytes() == b"foreign uv/pipx executable"


def posix_install(
    directory: Path,
    *,
    version: str = "2.0.2",
    scenario: str = "ok",
    delete: bool = False,
    bundle_directory: Path | None = None,
    use_curl: bool = False,
    stderr_fd: int | None = None,
) -> subprocess.CompletedProcess[str]:
    source = directory / "fixture/nesk"
    (source / "_internal").mkdir(parents=True, exist_ok=True)
    executable = source / "nesk"
    executable.write_text(
        "#!/bin/sh\n"
        + (
            "exit 71\n"
            if scenario == "smoke"
            else f'case "$1" in --version) echo "Luminesk {version}" ;; echo) shift; printf "%s\\n" "$@"; cat; exit 37 ;; esac\n'
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    if bundle_directory is not None:
        shutil.copytree(bundle_directory, source, dirs_exist_ok=True, symlinks=True)
    archive = directory / "bundle.zip"
    archive_bundle(source, archive)
    if scenario == "zip":
        archive.write_bytes(b"not a zip")
    corrupt_archive(archive, scenario)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (directory / "release").write_text(json.dumps({"tag_name": f"v{version}"}))
    digest = "0" * 64 if scenario == "sha" else digest
    target_os = "macos" if sys.platform == "darwin" else "linux"
    (directory / "sums").write_text(f"{digest}  luminesk-{target_os}-amd64.zip\n")
    installer = (REPOSITORY_ROOT / "assets/install.sh").read_text(encoding="utf-8")
    installer = installer.replace('if [ "$(id -u)" = 0 ]; then', "if false; then")
    if not use_curl:
        installer = installer.replace("fetch() {", "original_fetch() {")
    installer = installer.replace(
        'case "$(uname -s)"',
        'case "Darwin"' if sys.platform == "darwin" else 'case "Linux"',
    ).replace('case "$(uname -m)"', 'case "x86_64"')
    if scenario == "switch":
        installer = installer.replace(
            'mv -f "$NEXT" "$TARGET"', "false # simulated activation failure\n:"
        )
    if scenario == "interrupt":
        installer = installer.replace('mv -f "$NEXT" "$TARGET"', "kill -KILL $$")
    if scenario == "post-switch":
        installer = installer.replace(
            '"$TARGET" --version | grep',
            'false # simulated post-switch failure\n"$TARGET" --version | grep',
        )
    if scenario == "lock":
        installer = installer.replace(
            'mkdir "$LOCK" 2>/dev/null', 'mkdir -p "$LOCK"\nmkdir "$LOCK" 2>/dev/null'
        )
    if scenario == "architecture":
        installer = installer.replace('case "x86_64"', 'case "unsupported"')
    prelude = """
fetch() {
    [ "$LUMINESK_TEST_SCENARIO" != network ] || return 1
    case "$1" in
        https://api.github.com/repos/farin-works/luminesk-cli/releases/latest|https://github.com/farin-works/luminesk-cli/releases/download/*) ;;
        *) echo 'unexpected release repository' >&2; return 1 ;;
    esac
    case "$1" in */latest) name=release ;; */SHA256SUMS) name=sums ;; *) name=bundle.zip ;; esac
    cp "$LUMINESK_TEST_DATA/$name" "$2"
}
"""
    harness = directory / "install-test.sh"
    harness.write_text(prelude + installer, encoding="utf-8")
    home = directory / "home space Юникод"
    home.mkdir(exist_ok=True)
    host_tools = directory / "host-tools"
    host_tools.mkdir(exist_ok=True)
    lsof = host_tools / "lsof"
    lsof.write_text(
        '#!/bin/sh\nif [ "$LUMINESK_TEST_SCENARIO" = busy ]; then echo 123; exit 0; fi\nexit 1\n'
    )
    lsof.chmod(0o755)
    if use_curl:
        curl = host_tools / "curl"
        curl.write_text(
            """#!/bin/sh
printf '%s\\n' "$@" >> "$LUMINESK_TEST_DATA/curl-args"
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) destination=$2; shift ;;
        -D) headers=$2; shift ;;
    esac
    url=$1
    shift
done
case "$url" in */latest) name=release ;; */SHA256SUMS) name=sums ;; *) name=bundle.zip ;; esac
cp "$LUMINESK_TEST_DATA/$name" "$destination"
: > "$headers"
printf 200
""",
            encoding="utf-8",
        )
        curl.chmod(0o755)
    env = dict(
        os.environ,
        HOME=str(home),
        LUMINESK_TEST_DATA=str(directory),
        LUMINESK_TEST_SCENARIO=scenario,
    )
    env["PATH"] = (
        str(host_tools)
        + ":"
        + str(home / ".local/bin")
        + ":"
        + ":".join(
            part
            for part in env["PATH"].split(":")
            if not (Path(part) / "nesk").exists()
        )
    )
    return subprocess.run(
        ["sh", str(harness), "--yes", *(["--delete"] if delete else [])],
        env=env,
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=stderr_fd if stderr_fd is not None else subprocess.PIPE,
        text=True,
        check=False,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal integration")
@pytest.mark.parametrize(
    ("terminal", "term"), [(False, "xterm"), (True, "xterm"), (True, "dumb")]
)
def test_posix_download_progress_keeps_network_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: bool, term: str
) -> None:
    import pty

    monkeypatch.setenv("TERM", term)
    with_fds = pty.openpty() if terminal else None
    try:
        result = posix_install(
            tmp_path, use_curl=True, stderr_fd=with_fds[1] if with_fds else None
        )
    finally:
        if with_fds:
            os.close(with_fds[0])
            os.close(with_fds[1])
    assert result.returncode == 0, result.stdout + (result.stderr or "")
    arguments = (tmp_path / "curl-args").read_text().splitlines()
    progress = terminal and term != "dumb"
    assert arguments.count("--progress-bar") == int(progress)
    assert arguments.count("--silent") == (2 if progress else 3)
    # Progress changes only presentation, including when curl's HTTP code is
    # captured by the installer. All three requests keep the transport limits.
    assert arguments.count("--show-error") == 3
    for option, value in (
        ("--proto", "=https"),
        ("--connect-timeout", "20"),
        ("--max-time", "300"),
        ("--max-redirs", "0"),
        ("-w", "%{http_code}"),
    ):
        values = [arguments[i + 1] for i, arg in enumerate(arguments) if arg == option]
        assert values == [value] * 3
    limits = [
        arguments[i + 1] for i, arg in enumerate(arguments) if arg == "--max-filesize"
    ]
    assert limits == ["4194304", "1048576", "536870912"]


def assert_installation_output(
    output: str, version: str, installed: str | None
) -> None:
    messages = (
        "Luminesk installer",
        "Checking system and existing installation...",
        "Checking the latest release...",
        "Installation summary",
        "Downloading Luminesk",
        "Verifying archive checksum...",
        "Archive checksum verified.",
        "Extracting and verifying bundle files...",
        "Checking application imports, version and help...",
        "Activating the verified application...",
        "Configuring PATH...",
        f"Luminesk {version} installed.",
        "Next steps",
        "nesk --version",
        "docker version",
        "nesk doctor",
        "nesk --help",
    )
    positions = [output.index(message) for message in messages]
    assert positions == sorted(positions)
    assert f"Latest release : {version}" in output
    if installed is None:
        assert "Installed      : Not installed" in output
        assert "Status         : New installation" in output
    else:
        assert f"Installed      : Luminesk {installed}" in output
        status = (
            "Up to date (reinstall)"
            if version == installed
            else f"Version change ({installed} -> {version})"
        )
        assert f"Status         : {status}" in output
    assert "Command        : " in output
    assert "\x1b[" not in output


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer integration")
def test_posix_install_update_reinstall_and_remove(tmp_path: Path) -> None:
    for version, installed in (("2.0.2", None), ("2.0.3", "2.0.2"), ("2.0.3", "2.0.3")):
        result = posix_install(tmp_path, version=version)
        assert result.returncode == 0, result.stdout + result.stderr
        assert_installation_output(result.stdout, version, installed)
        if sys.platform == "darwin":
            assert "macos-amd64" in result.stdout
            assert "Docker Desktop" in result.stdout
        else:
            assert "linux-amd64" in result.stdout
            assert (
                "--set runtime_uid=$(id -u) --set runtime_gid=$(id -g)" in result.stdout
            )
    home = tmp_path / "home space Юникод"
    launcher = home / ".local/bin/nesk"
    assert launcher.is_symlink()
    echoed = subprocess.run(
        [str(launcher), "echo", "two words", "Юникод", "a&b"],
        input="stdin\n",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert echoed.returncode == 37
    assert echoed.stdout == "two words\nЮникод\na&b\nstdin\n"
    other = home / ".local/bin/other"
    other.write_text("keep")
    result = posix_install(tmp_path, delete=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Removing installer-owned application files..." in result.stdout
    assert "instances and cache were preserved" in result.stdout
    assert "Next steps" not in result.stdout
    assert not launcher.is_symlink()
    assert other.read_text() == "keep"


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer integration")
@pytest.mark.parametrize(
    "scenario",
    [
        "sha",
        "zip",
        "smoke",
        "network",
        "switch",
        "post-switch",
        "lock",
        "architecture",
        "traversal",
        "absolute",
        "duplicate",
        "link",
        "oversize",
        "manifest-traversal",
    ],
)
def test_posix_failed_update_keeps_old_version(tmp_path: Path, scenario: str) -> None:
    result = posix_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = tmp_path / "home space Юникод/.local/bin/nesk"
    old = launcher.readlink()
    result = posix_install(tmp_path, scenario=scenario, version="2.0.3")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Luminesk 2.0.3 installed." not in result.stdout
    assert "Next steps" not in result.stdout
    assert launcher.readlink() == old


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer integration")
def test_posix_migrates_onefile_and_rejects_foreign(tmp_path: Path) -> None:
    home = tmp_path / "home space Юникод"
    binary = home / ".local/bin/nesk"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\necho "Luminesk 2.0.1"\n')
    binary.chmod(0o755)
    result = posix_install(tmp_path)
    assert result.returncode != 0
    assert not binary.is_symlink()
    metadata = home / ".local/share/luminesk/.metadata_one_line"
    metadata.write_text(
        f"installed_via=one-line-script\nbinary={binary}\nversion=2.0.1\n",
        encoding="utf-8",
    )
    result = posix_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert binary.is_symlink()


def test_windows_interrupted_switch_can_resume(
    tmp_path: Path, windows_fixture_exe: Path
) -> None:
    result = windows_install(tmp_path, windows_fixture_exe)
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = tmp_path / "home space Юникод/.local/bin/nesk.cmd"
    before = launcher.read_bytes()
    result = windows_install(
        tmp_path, windows_fixture_exe, scenario="interrupt", version="2.0.3"
    )
    assert result.returncode == 130
    assert launcher.read_bytes() == before
    result = windows_install(tmp_path, windows_fixture_exe, version="2.0.3")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX interruption and stale lock")
def test_posix_interruption_preserves_command_and_recovery(tmp_path: Path) -> None:
    result = posix_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = tmp_path / "home space Юникод/.local/bin/nesk"
    before = launcher.readlink()
    result = posix_install(tmp_path, scenario="interrupt", version="2.0.3")
    assert result.returncode < 0
    assert launcher.readlink() == before
    result = posix_install(tmp_path, version="2.0.3")
    assert result.returncode != 0
    lock = tmp_path / "home space Юникод/.local/share/luminesk/app/install.lock"
    lock.rmdir()  # The terminated subprocess is reaped; documented recovery is safe.
    result = posix_install(tmp_path, version="2.0.3")
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_bundle_installs_through_official_script(tmp_path: Path) -> None:
    configured = os.environ.get("LUMINESK_TEST_BUNDLE")
    if not configured:
        pytest.skip("Set LUMINESK_TEST_BUNDLE to exercise an actual PyInstaller bundle")
    from luminesk_cli._version import __version__

    bundle = Path(configured).resolve()
    if os.name == "nt":
        result = windows_install(
            tmp_path, bundle / "nesk.exe", version=__version__, bundle_directory=bundle
        )
    else:
        result = posix_install(tmp_path, version=__version__, bundle_directory=bundle)
    assert result.returncode == 0, result.stdout + result.stderr


def test_windows_launcher_preserves_arguments_streams_and_exit(
    tmp_path: Path, windows_fixture_exe: Path
) -> None:
    result = windows_install(tmp_path, windows_fixture_exe)
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = tmp_path / "home space Юникод/.local/bin/nesk.cmd"
    result = subprocess.run(
        [str(launcher), "echo", "two words", "Юникод"],
        cwd=tmp_path,
        input="stdin\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 37
    assert result.stdout == "two words\nЮникод\nstdin\n"
    assert result.stderr == "stderr preserved\n"


def test_windows_migration_failure_restores_onefile(
    tmp_path: Path, windows_fixture_exe: Path
) -> None:
    home = tmp_path / "home space Юникод"
    binary = home / ".local/bin/nesk.exe"
    binary.parent.mkdir(parents=True)
    shutil.copyfile(windows_fixture_exe, binary)
    (binary.parent / "version").write_text("2.0.1")
    metadata = home / ".local/share/luminesk/.metadata_one_line"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        f"installed_via=one-line-script\nbinary={binary}\nversion=2.0.1\n",
        encoding="utf-8",
    )
    before = metadata.read_bytes()
    result = windows_install(tmp_path, windows_fixture_exe, scenario="post-switch")
    assert result.returncode != 0
    assert binary.read_bytes() == windows_fixture_exe.read_bytes()
    assert metadata.read_bytes() == before
    assert not (binary.parent / "nesk.cmd").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink extraction")
def test_posix_installer_restores_safe_symlinks(tmp_path: Path) -> None:
    library = tmp_path / "fixture/nesk/_internal/library"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"real library")
    (library.parent / "alias").symlink_to("library")
    result = posix_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    active = (tmp_path / "home space Юникод/.local/bin/nesk").resolve().parent
    assert (active / "_internal/alias").is_symlink()
    assert (active / "_internal/alias").read_bytes() == b"real library"


@pytest.mark.skipif(os.name == "nt", reason="POSIX open-file guard")
def test_posix_uninstall_refuses_running_application(tmp_path: Path) -> None:
    result = posix_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = tmp_path / "home space Юникод/.local/bin/nesk"
    previous = launcher.readlink()
    result = posix_install(tmp_path, scenario="busy", delete=True)
    assert result.returncode != 0
    assert launcher.readlink() == previous
    assert launcher.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX metadata ownership")
def test_posix_metadata_for_another_binary_does_not_grant_ownership(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home space Юникод"
    binary = home / ".local/bin/nesk"
    binary.parent.mkdir(parents=True)
    binary.write_text("foreign")
    binary.chmod(0o755)
    metadata = home / ".local/share/luminesk/.metadata_one_line"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("installed_via=one-line-script\nbinary=/some/other/nesk\n")
    result = posix_install(tmp_path)
    assert result.returncode != 0
    assert binary.read_text() == "foreign"


def test_windows_metadata_for_another_binary_does_not_grant_ownership(
    tmp_path: Path, windows_fixture_exe: Path
) -> None:
    home = tmp_path / "home space Юникод"
    binary = home / ".local/bin/nesk.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"foreign")
    metadata = home / ".local/share/luminesk/.metadata_one_line"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("installed_via=one-line-script\nbinary=C:\\other\\nesk.exe\n")
    result = windows_install(tmp_path, windows_fixture_exe)
    assert result.returncode != 0
    assert binary.read_bytes() == b"foreign"


def test_windows_uninstall_preserves_user_files_inside_application(
    tmp_path: Path, windows_fixture_exe: Path
) -> None:
    result = windows_install(tmp_path, windows_fixture_exe)
    assert result.returncode == 0, result.stdout + result.stderr
    home = tmp_path / "home space Юникод"
    version = next((home / ".local/share/luminesk/app/versions").iterdir())
    world = version / "nesk/world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"user world")
    result = windows_install(tmp_path, windows_fixture_exe, delete=True)
    assert result.returncode != 0
    assert (home / ".local/bin/nesk.cmd").is_file()
    assert (world / "level.dat").read_bytes() == b"user world"


@pytest.mark.skipif(os.name == "nt", reason="POSIX removal ownership")
def test_posix_uninstall_preserves_user_files_inside_application(
    tmp_path: Path,
) -> None:
    result = posix_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = tmp_path / "home space Юникод/.local/bin/nesk"
    world = launcher.resolve().parent / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"user world")
    result = posix_install(tmp_path, delete=True)
    assert result.returncode != 0
    assert launcher.exists()
    assert (world / "level.dat").read_bytes() == b"user world"
