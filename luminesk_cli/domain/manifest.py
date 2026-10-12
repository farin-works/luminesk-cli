"""Strict public schema v1 loader for ``luminesk.toml``."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from luminesk_cli.domain.errors import ValidationError
from luminesk_cli.domain.primitives import (
    PACKAGE_NAME_RE,
    PLATFORM_RE,
    SEMVER_RE,
    fail,
    optional_string,
    read_bounded_utf8,
    reject_unknown,
    require_array,
    require_bool,
    require_int,
    require_keys,
    require_string,
    require_table,
    safe_relative_path,
    sha256_digest,
    validate_https_url,
)

MANIFEST_NAME = "luminesk.toml"
MAX_MANIFEST_SIZE = 1024 * 1024
DEFAULT_SOURCE_MAX_SIZE = 536_870_912
SOURCE_TYPES = frozenset(
    {
        "http",
        "maven",
        "jenkins",
        "github-release",
        "github-source",
        "gitlab-release",
        "gitlab-job-artifact",
        "mojang-version",
        "paper",
        "local-file",
    }
)
FILE_MODES = frozenset({"managed", "preserve", "generated", "data"})
CHECK_PHASES = frozenset({"post-build", "post-install", "readiness"})
CHECK_KINDS = frozenset({"file", "process-alive", "log-regex", "tcp", "command"})
PACKAGE_KINDS = frozenset({"core", "template"})
MINECRAFT_EDITIONS = frozenset({"java", "bedrock", "cross-platform"})
INPUT_REFERENCE_RE = re.compile(r"\$\{input\.([A-Za-z0-9_-]+)}")


@dataclass(slots=True, frozen=True)
class Repository:
    url: str


@dataclass(slots=True, frozen=True)
class Package:
    name: str
    version: str
    kind: Literal["core", "template"]
    game: Literal["minecraft"]
    edition: Literal["java", "bedrock", "cross-platform"]
    display_name: str | None = None
    summary: str = ""
    keywords: tuple[str, ...] = ()
    license: str | None = None
    authors: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    repository: Repository | None = None

    @property
    def description(self) -> str:
        """Internal compatibility alias; ``summary`` is the public v1 field."""

        return self.summary


@dataclass(slots=True, frozen=True)
class InputSpec:
    name: str
    type: Literal["string", "integer", "boolean"]
    default: str | int | bool | None = None
    prompt: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    pattern: str | None = None
    required: bool = False
    secret: bool = False


@dataclass(slots=True, frozen=True)
class HttpOptions:
    url: str
    version: str = "pinned"


@dataclass(slots=True, frozen=True)
class MavenOptions:
    repository: str
    group: str
    artifact: str
    version: str
    extension: str = "jar"
    classifier: str | None = None
    channel: str = "stable"


@dataclass(slots=True, frozen=True)
class JenkinsOptions:
    base_url: str
    job: str
    artifact: str
    build: str | int = "lastSuccessfulBuild"


@dataclass(slots=True, frozen=True)
class GitHubReleaseOptions:
    repository: str
    asset: str
    version: str = "latest"
    channel: str = "stable"


@dataclass(slots=True, frozen=True)
class GitHubSourceOptions:
    repository: str
    ref: str = "main"
    path: str | None = None


@dataclass(slots=True, frozen=True)
class GitLabReleaseOptions:
    project: str
    asset: str
    version: str = "latest"
    base_url: str = "https://gitlab.com"


@dataclass(slots=True, frozen=True)
class GitLabJobArtifactOptions:
    project: str
    job: str
    artifact: str
    ref: str = "main"
    base_url: str = "https://gitlab.com"


@dataclass(slots=True, frozen=True)
class MojangVersionOptions:
    version: str = "latest"


@dataclass(slots=True, frozen=True)
class PaperOptions:
    minecraft: str
    build: str | int = "latest"


@dataclass(slots=True, frozen=True)
class LocalFileOptions:
    path: str
    version: str = "local"


type SourceOptions = (
    HttpOptions
    | MavenOptions
    | JenkinsOptions
    | GitHubReleaseOptions
    | GitHubSourceOptions
    | GitLabReleaseOptions
    | GitLabJobArtifactOptions
    | MojangVersionOptions
    | PaperOptions
    | LocalFileOptions
)


@dataclass(slots=True, frozen=True)
class SourceSpec:
    id: str
    type: str
    target: str
    options: SourceOptions
    max_size: int = DEFAULT_SOURCE_MAX_SIZE
    extract: bool = False
    platforms: tuple[str, ...] = ()
    allow_http: bool = False
    allow_private_network: bool = False


@dataclass(slots=True, frozen=True)
class FileSpec:
    source: str
    target: str
    mode: Literal["managed", "preserve", "generated", "data"] = "managed"
    template: bool = False
    executable: bool = False


@dataclass(slots=True, frozen=True)
class Ownership:
    preserve: tuple[str, ...] = ()
    data: tuple[str, ...] = ()
    executable: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class RuntimeMount:
    source: str
    target: str
    mode: Literal["ro", "rw"] = "rw"


@dataclass(slots=True, frozen=True)
class RuntimePort:
    name: str
    host: int | str
    container: int | str
    protocol: Literal["tcp", "udp"] = "tcp"


@dataclass(slots=True, frozen=True)
class Runtime:
    image: str
    command: tuple[str, ...]
    workdir: str = "/server"
    memory: str | None = None
    stop_signal: str = "SIGINT"
    stop_timeout: int = 30
    restart: str = "no"
    restart_limit: int = 0
    run_as: str | None = None
    read_only_root: bool = True
    mounts: tuple[RuntimeMount, ...] = ()
    ports: tuple[RuntimePort, ...] = ()


@dataclass(slots=True, frozen=True)
class Build:
    file: str
    output: str
    timeout: int = 1200
    cpu: int = 2
    memory: str = "2g"
    network: bool = False


@dataclass(slots=True, frozen=True)
class Check:
    id: str
    phase: str
    kind: str
    required: bool = True
    path: str | None = None
    pattern: str | None = None
    host: str | None = None
    port: int | str | None = None
    command: tuple[str, ...] = ()
    timeout: int = 30


@dataclass(slots=True, frozen=True)
class UpdatePolicy:
    strategy: Literal["transactional"] = "transactional"
    backup: tuple[str, ...] = ()
    retain_backups: int = 3
    rollback_on_failure: bool = True


@dataclass(slots=True, frozen=True)
class Manifest:
    manifest_version: Literal[1]
    package: Package
    runtime: Runtime
    sources: tuple[SourceSpec, ...] = ()
    template: str | None = None
    ownership: Ownership = field(default_factory=Ownership)
    inputs: tuple[InputSpec, ...] = ()
    files: tuple[FileSpec, ...] = ()
    build: Build | None = None
    checks: tuple[Check, ...] = ()
    update: UpdatePolicy = field(default_factory=UpdatePolicy)
    extensions: dict[str, Any] = field(default_factory=dict)
    digest: str = ""


def _string_array(value: Any, path: str) -> tuple[str, ...]:
    return tuple(
        require_string(item, f"{path}[{index}]")
        for index, item in enumerate(require_array(value, path))
    )


def _parse_package(value: Any) -> Package:
    path = "package"
    table = require_table(value, path)
    reject_unknown(
        table,
        {
            "name",
            "version",
            "display_name",
            "kind",
            "game",
            "edition",
            "summary",
            "keywords",
            "license",
            "authors",
            "platforms",
            "repository",
        },
        path,
    )
    require_keys(table, {"name", "version", "kind", "game", "edition"}, path)
    name = require_string(table["name"], f"{path}.name")
    version = require_string(table["version"], f"{path}.version")
    kind = require_string(table["kind"], f"{path}.kind")
    game = require_string(table["game"], f"{path}.game")
    edition = require_string(table["edition"], f"{path}.edition")

    if not PACKAGE_NAME_RE.fullmatch(name):
        fail(f"{path}.name", "must be a lowercase package identifier")
    if not SEMVER_RE.fullmatch(version):
        fail(f"{path}.version", "must be a semantic version")
    if kind not in PACKAGE_KINDS:
        fail(f"{path}.kind", "must be core or template")
    if game != "minecraft":
        fail(f"{path}.game", "must be minecraft")
    if edition not in MINECRAFT_EDITIONS:
        fail(f"{path}.edition", "must be java, bedrock, or cross-platform")

    platforms = _string_array(table.get("platforms", []), f"{path}.platforms")
    _validate_platforms(platforms, f"{path}.platforms")
    repository = None

    if "repository" in table:
        repository_table = require_table(table["repository"], f"{path}.repository")
        reject_unknown(repository_table, {"url"}, f"{path}.repository")
        require_keys(repository_table, {"url"}, f"{path}.repository")
        repository = Repository(
            validate_https_url(repository_table["url"], f"{path}.repository.url")
        )

    return Package(
        name=name,
        version=version,
        display_name=optional_string(table, "display_name", path),
        kind=cast(Literal["core", "template"], kind),
        game="minecraft",
        edition=cast(Literal["java", "bedrock", "cross-platform"], edition),
        summary=require_string(
            table.get("summary", ""), f"{path}.summary", non_empty=False
        ),
        keywords=_string_array(table.get("keywords", []), f"{path}.keywords"),
        license=optional_string(table, "license", path),
        authors=_string_array(table.get("authors", []), f"{path}.authors"),
        platforms=platforms,
        repository=repository,
    )


def _validate_platforms(platforms: tuple[str, ...], path: str) -> None:
    for index, platform in enumerate(platforms):
        if not PLATFORM_RE.fullmatch(platform):
            fail(f"{path}[{index}]", "expected os/architecture")


def _parse_inputs(value: Any) -> tuple[InputSpec, ...]:
    table = require_table(value, "inputs")
    result = []

    for name, raw_input in table.items():
        path = f"inputs.{name}"
        spec = require_table(raw_input, path)
        reject_unknown(
            spec,
            {
                "type",
                "default",
                "prompt",
                "min",
                "max",
                "pattern",
                "required",
                "secret",
            },
            path,
        )
        require_keys(spec, {"type"}, path)
        input_type = require_string(spec["type"], f"{path}.type")
        if input_type not in {"string", "integer", "boolean"}:
            fail(f"{path}.type", "must be string, integer, or boolean")

        default = spec.get("default")
        if default is not None:
            expected = {"string": str, "integer": int, "boolean": bool}[input_type]
            if not isinstance(default, expected) or (
                input_type == "integer" and isinstance(default, bool)
            ):
                fail(f"{path}.default", f"expected {input_type}")

        minimum = require_int(spec["min"], f"{path}.min") if "min" in spec else None
        maximum = require_int(spec["max"], f"{path}.max") if "max" in spec else None
        if minimum is not None and maximum is not None and minimum > maximum:
            fail(path, "min may not exceed max")

        pattern = optional_string(spec, "pattern", path)
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as exc:
                fail(f"{path}.pattern", f"invalid regular expression: {exc}")

        if isinstance(default, int) and not isinstance(default, bool):
            if minimum is not None and default < minimum:
                fail(f"{path}.default", "is below min")
            if maximum is not None and default > maximum:
                fail(f"{path}.default", "is above max")

        secret = require_bool(spec.get("secret", False), f"{path}.secret")
        if secret and default is not None:
            fail(f"{path}.default", "secret inputs may not declare a default")

        result.append(
            InputSpec(
                name=name,
                type=input_type,
                default=cast(str | int | bool | None, default),
                prompt=optional_string(spec, "prompt", path),
                minimum=minimum,
                maximum=maximum,
                pattern=pattern,
                required=require_bool(spec.get("required", False), f"{path}.required"),
                secret=secret,
            )
        )

    return tuple(result)


def _github_repository(value: Any, path: str) -> str:
    repository = require_string(value, path).strip().strip("/")
    parts = repository.split("/")
    if len(parts) != 2 or not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts
    ):
        fail(path, "must be OWNER/REPO")
    return repository


def _source_url(value: Any, path: str, allow_http: bool) -> str:
    return validate_https_url(value, path, allow_http=allow_http)


def _parse_source_options(
    source_type: str,
    value: Any,
    path: str,
    *,
    allow_http: bool,
) -> SourceOptions:
    table = require_table(value, path)

    if source_type == "http":
        reject_unknown(table, {"url", "version"}, path)
        require_keys(table, {"url"}, path)
        return HttpOptions(
            url=_source_url(table["url"], f"{path}.url", allow_http),
            version=require_string(table.get("version", "pinned"), f"{path}.version"),
        )

    if source_type == "maven":
        reject_unknown(
            table,
            {
                "repository",
                "group",
                "artifact",
                "version",
                "extension",
                "classifier",
                "channel",
            },
            path,
        )
        require_keys(table, {"repository", "group", "artifact", "version"}, path)
        return MavenOptions(
            repository=_source_url(
                table["repository"], f"{path}.repository", allow_http
            ),
            group=require_string(table["group"], f"{path}.group"),
            artifact=require_string(table["artifact"], f"{path}.artifact"),
            version=require_string(table["version"], f"{path}.version"),
            extension=require_string(
                table.get("extension", "jar"), f"{path}.extension"
            ),
            classifier=optional_string(table, "classifier", path),
            channel=require_string(table.get("channel", "stable"), f"{path}.channel"),
        )

    if source_type == "jenkins":
        reject_unknown(table, {"base_url", "job", "build", "artifact"}, path)
        require_keys(table, {"base_url", "job", "artifact"}, path)
        build = table.get("build", "lastSuccessfulBuild")
        if isinstance(build, bool) or not isinstance(build, (str, int)):
            fail(f"{path}.build", "expected a string or integer")
        return JenkinsOptions(
            base_url=_source_url(table["base_url"], f"{path}.base_url", allow_http),
            job=require_string(table["job"], f"{path}.job"),
            artifact=safe_relative_path(table["artifact"], f"{path}.artifact"),
            build=build,
        )

    if source_type == "github-release":
        reject_unknown(table, {"repository", "version", "asset", "channel"}, path)
        require_keys(table, {"repository", "asset"}, path)
        return GitHubReleaseOptions(
            repository=_github_repository(table["repository"], f"{path}.repository"),
            version=require_string(table.get("version", "latest"), f"{path}.version"),
            asset=require_string(table["asset"], f"{path}.asset"),
            channel=require_string(table.get("channel", "stable"), f"{path}.channel"),
        )

    if source_type == "github-source":
        reject_unknown(table, {"repository", "ref", "path"}, path)
        require_keys(table, {"repository"}, path)
        return GitHubSourceOptions(
            repository=_github_repository(table["repository"], f"{path}.repository"),
            ref=require_string(table.get("ref", "main"), f"{path}.ref"),
            path=(
                safe_relative_path(table["path"], f"{path}.path")
                if "path" in table
                else None
            ),
        )

    if source_type == "gitlab-release":
        reject_unknown(table, {"base_url", "project", "version", "asset"}, path)
        require_keys(table, {"project", "asset"}, path)
        return GitLabReleaseOptions(
            base_url=_source_url(
                table.get("base_url", "https://gitlab.com"),
                f"{path}.base_url",
                allow_http,
            ),
            project=require_string(table["project"], f"{path}.project"),
            version=require_string(table.get("version", "latest"), f"{path}.version"),
            asset=require_string(table["asset"], f"{path}.asset"),
        )

    if source_type == "gitlab-job-artifact":
        reject_unknown(table, {"base_url", "project", "ref", "job", "artifact"}, path)
        require_keys(table, {"project", "job", "artifact"}, path)
        return GitLabJobArtifactOptions(
            base_url=_source_url(
                table.get("base_url", "https://gitlab.com"),
                f"{path}.base_url",
                allow_http,
            ),
            project=require_string(table["project"], f"{path}.project"),
            ref=require_string(table.get("ref", "main"), f"{path}.ref"),
            job=require_string(table["job"], f"{path}.job"),
            artifact=safe_relative_path(table["artifact"], f"{path}.artifact"),
        )

    if source_type == "mojang-version":
        reject_unknown(table, {"version"}, path)
        return MojangVersionOptions(
            version=require_string(table.get("version", "latest"), f"{path}.version")
        )

    if source_type == "paper":
        reject_unknown(table, {"minecraft", "build"}, path)
        require_keys(table, {"minecraft"}, path)
        build = table.get("build", "latest")
        if isinstance(build, bool) or not isinstance(build, (str, int)):
            fail(f"{path}.build", "expected a string or integer")
        return PaperOptions(
            minecraft=require_string(table["minecraft"], f"{path}.minecraft"),
            build=build,
        )

    if source_type == "local-file":
        reject_unknown(table, {"path", "version"}, path)
        require_keys(table, {"path"}, path)
        return LocalFileOptions(
            path=safe_relative_path(table["path"], f"{path}.path"),
            version=require_string(table.get("version", "local"), f"{path}.version"),
        )

    fail(
        path.removesuffix(".options") + ".type",
        f"unsupported source type: {source_type}",
    )


def _parse_sources(value: Any) -> tuple[SourceSpec, ...]:
    result = []
    seen_ids: set[str] = set()
    seen_targets: set[str] = set()

    for index, raw_source in enumerate(require_array(value, "sources")):
        path = f"sources[{index}]"
        table = require_table(raw_source, path)
        reject_unknown(
            table,
            {
                "id",
                "type",
                "target",
                "max_size",
                "extract",
                "platforms",
                "allow_http",
                "allow_private_network",
                "options",
            },
            path,
        )
        require_keys(table, {"id", "type", "target", "options"}, path)
        source_id = require_string(table["id"], f"{path}.id")
        source_type = require_string(table["type"], f"{path}.type")
        target = safe_relative_path(table["target"], f"{path}.target", allow_dot=True)
        if not PACKAGE_NAME_RE.fullmatch(source_id):
            fail(f"{path}.id", "must be a lowercase identifier")
        if source_id in seen_ids:
            fail(f"{path}.id", "source ids must be unique")
        if target in seen_targets:
            fail(f"{path}.target", "source targets must be unique")
        if source_type not in SOURCE_TYPES:
            fail(f"{path}.type", f"unsupported source type: {source_type}")
        seen_ids.add(source_id)
        seen_targets.add(target)
        allow_http = require_bool(table.get("allow_http", False), f"{path}.allow_http")
        extract = require_bool(table.get("extract", False), f"{path}.extract")
        if target == "." and not extract:
            fail(f"{path}.target", "'.' is allowed only for extracted sources")
        if source_type == "github-source" and not extract:
            fail(f"{path}.extract", "github-source must be extracted")
        platforms = _string_array(table.get("platforms", []), f"{path}.platforms")
        _validate_platforms(platforms, f"{path}.platforms")
        result.append(
            SourceSpec(
                id=source_id,
                type=source_type,
                target=target,
                options=_parse_source_options(
                    source_type,
                    table["options"],
                    f"{path}.options",
                    allow_http=allow_http,
                ),
                max_size=require_int(
                    table.get("max_size", DEFAULT_SOURCE_MAX_SIZE),
                    f"{path}.max_size",
                    minimum=1,
                ),
                extract=extract,
                platforms=platforms,
                allow_http=allow_http,
                allow_private_network=require_bool(
                    table.get("allow_private_network", False),
                    f"{path}.allow_private_network",
                ),
            )
        )

    return tuple(result)


def _parse_files(value: Any) -> tuple[FileSpec, ...]:
    result = []
    targets = set()
    for index, raw_file in enumerate(require_array(value, "files")):
        path = f"files[{index}]"
        table = require_table(raw_file, path)
        reject_unknown(
            table, {"source", "target", "mode", "template", "executable"}, path
        )
        require_keys(table, {"source", "target"}, path)
        target = safe_relative_path(table["target"], f"{path}.target")
        mode = require_string(table.get("mode", "managed"), f"{path}.mode")
        if target in targets:
            fail(f"{path}.target", "file targets must be unique")
        if mode not in FILE_MODES:
            fail(f"{path}.mode", "unsupported ownership mode")
        targets.add(target)
        result.append(
            FileSpec(
                source=safe_relative_path(table["source"], f"{path}.source"),
                target=target,
                mode=cast(Literal["managed", "preserve", "generated", "data"], mode),
                template=require_bool(table.get("template", False), f"{path}.template"),
                executable=require_bool(
                    table.get("executable", False), f"{path}.executable"
                ),
            )
        )
    return tuple(result)


def _parse_ownership(value: Any) -> Ownership:
    path = "ownership"
    table = require_table(value, path)
    reject_unknown(table, {"preserve", "data", "executable"}, path)
    parsed: dict[str, tuple[str, ...]] = {}
    for policy in ("preserve", "data", "executable"):
        values = tuple(
            _ownership_path(item, f"{path}.{policy}[{index}]")
            for index, item in enumerate(
                require_array(table.get(policy, []), f"{path}.{policy}")
            )
        )
        if len(values) != len(set(values)):
            fail(f"{path}.{policy}", "duplicate policy path")
        parsed[policy] = values

    for preserved in parsed["preserve"]:
        for data in parsed["data"]:
            if _paths_overlap(preserved, data):
                fail(
                    path,
                    f"overlapping preserve/data policy paths: {preserved}, {data}",
                )
    return Ownership(**parsed)  # type: ignore[arg-type]


def _ownership_path(value: Any, path: str) -> str:
    result = safe_relative_path(value, path)
    if any(character in result for character in "*?[]"):
        fail(path, "globs are not supported")
    return result


def _paths_overlap(first: str, second: str) -> bool:
    return (
        first == second
        or first.startswith(f"{second}/")
        or second.startswith(f"{first}/")
    )


def _parse_runtime(value: Any) -> Runtime:
    path = "runtime"
    table = require_table(value, path)
    reject_unknown(
        table,
        {
            "image",
            "command",
            "workdir",
            "memory",
            "stop_signal",
            "stop_timeout",
            "restart",
            "restart_limit",
            "run_as",
            "read_only_root",
            "mounts",
            "ports",
        },
        path,
    )
    require_keys(table, {"image", "command"}, path)
    command = tuple(
        require_string(item, f"{path}.command[{index}]")
        for index, item in enumerate(require_array(table["command"], f"{path}.command"))
    )
    if not command:
        fail(f"{path}.command", "must contain at least one argv element")

    mounts = []
    for index, raw_mount in enumerate(
        require_array(table.get("mounts", []), f"{path}.mounts")
    ):
        item_path = f"{path}.mounts[{index}]"
        mount = require_table(raw_mount, item_path)
        reject_unknown(mount, {"source", "target", "mode"}, item_path)
        require_keys(mount, {"source", "target"}, item_path)
        mode = require_string(mount.get("mode", "rw"), f"{item_path}.mode")
        if mode not in {"ro", "rw"}:
            fail(f"{item_path}.mode", "must be ro or rw")
        mounts.append(
            RuntimeMount(
                source=safe_relative_path(
                    mount["source"], f"{item_path}.source", allow_dot=True
                ),
                target=require_string(mount["target"], f"{item_path}.target"),
                mode=mode,
            )
        )

    ports = []
    seen_container_bindings: set[tuple[int | str, str]] = set()
    for index, raw_port in enumerate(
        require_array(table.get("ports", []), f"{path}.ports")
    ):
        item_path = f"{path}.ports[{index}]"
        port = require_table(raw_port, item_path)
        reject_unknown(port, {"name", "host", "container", "protocol"}, item_path)
        require_keys(port, {"name", "host", "container"}, item_path)
        protocol = require_string(port.get("protocol", "tcp"), f"{item_path}.protocol")
        if protocol not in {"tcp", "udp"}:
            fail(f"{item_path}.protocol", "must be tcp or udp")
        host = _parse_port_value(port["host"], f"{item_path}.host")
        container = _parse_port_value(port["container"], f"{item_path}.container")
        container_binding = (container, protocol)
        if container_binding in seen_container_bindings:
            fail(f"{item_path}.container", "container port bindings must be unique")
        seen_container_bindings.add(container_binding)
        ports.append(
            RuntimePort(
                name=require_string(port["name"], f"{item_path}.name"),
                host=host,
                container=container,
                protocol=protocol,
            )
        )

    return Runtime(
        image=require_string(table["image"], f"{path}.image"),
        command=command,
        workdir=require_string(table.get("workdir", "/server"), f"{path}.workdir"),
        memory=optional_string(table, "memory", path),
        stop_signal=require_string(
            table.get("stop_signal", "SIGINT"), f"{path}.stop_signal"
        ),
        stop_timeout=require_int(
            table.get("stop_timeout", 30), f"{path}.stop_timeout", minimum=1
        ),
        restart=require_string(table.get("restart", "no"), f"{path}.restart"),
        restart_limit=require_int(
            table.get("restart_limit", 0), f"{path}.restart_limit", minimum=0
        ),
        run_as=optional_string(table, "run_as", path),
        read_only_root=require_bool(
            table.get("read_only_root", True), f"{path}.read_only_root"
        ),
        mounts=tuple(mounts),
        ports=tuple(ports),
    )


def _parse_port_value(value: Any, path: str) -> int | str:
    if isinstance(value, int) and not isinstance(value, bool):
        return require_int(value, path, minimum=1, maximum=65535)
    text = require_string(value, path)
    if re.fullmatch(r"\$\{input\.[A-Za-z0-9_-]+}", text) is None:
        fail(path, "must be a port number or ${input.name}")
    return text


def _parse_build(value: Any) -> Build:
    path = "build"
    table = require_table(value, path)
    reject_unknown(
        table, {"file", "output", "timeout", "cpu", "memory", "network"}, path
    )
    require_keys(table, {"file", "output"}, path)
    output = require_string(table["output"], f"{path}.output")
    if not output.startswith("/"):
        fail(f"{path}.output", "must be an absolute container path")
    return Build(
        file=safe_relative_path(table["file"], f"{path}.file"),
        output=output,
        timeout=require_int(table.get("timeout", 1200), f"{path}.timeout", minimum=1),
        cpu=require_int(table.get("cpu", 2), f"{path}.cpu", minimum=1),
        memory=require_string(table.get("memory", "2g"), f"{path}.memory"),
        network=require_bool(table.get("network", False), f"{path}.network"),
    )


def _parse_checks(value: Any) -> tuple[Check, ...]:
    result = []
    seen = set()
    for index, raw_check in enumerate(require_array(value, "checks")):
        path = f"checks[{index}]"
        table = require_table(raw_check, path)
        reject_unknown(
            table,
            {
                "id",
                "phase",
                "kind",
                "required",
                "path",
                "pattern",
                "host",
                "port",
                "command",
                "timeout",
            },
            path,
        )
        require_keys(table, {"id", "phase", "kind"}, path)
        check_id = require_string(table["id"], f"{path}.id")
        phase = require_string(table["phase"], f"{path}.phase")
        kind = require_string(table["kind"], f"{path}.kind")
        if check_id in seen:
            fail(f"{path}.id", "check ids must be unique")
        if phase not in CHECK_PHASES:
            fail(f"{path}.phase", "unsupported check phase")
        if kind not in CHECK_KINDS:
            fail(f"{path}.kind", "unsupported check kind")
        seen.add(check_id)
        check_path = (
            safe_relative_path(table["path"], f"{path}.path")
            if "path" in table
            else None
        )
        pattern = optional_string(table, "pattern", path)
        if kind == "file" and check_path is None:
            fail(path, "file checks require path")
        if kind == "log-regex" and pattern is None:
            fail(path, "log-regex checks require pattern")
        command = tuple(
            require_string(item, f"{path}.command[{command_index}]")
            for command_index, item in enumerate(
                require_array(table.get("command", []), f"{path}.command")
            )
        )
        if kind == "command" and not command:
            fail(path, "command checks require a non-empty argv array")
        if phase in {"post-build", "post-install"} and kind != "file":
            fail(path, f"{phase} currently supports only file checks")
        if phase == "readiness" and kind == "file":
            fail(path, "readiness file checks must use post-install phase")
        port = table.get("port")
        if port is not None:
            port = _parse_port_value(port, f"{path}.port")
        result.append(
            Check(
                id=check_id,
                phase=phase,
                kind=kind,
                required=require_bool(table.get("required", True), f"{path}.required"),
                path=check_path,
                pattern=pattern,
                host=optional_string(table, "host", path),
                port=port,
                command=command,
                timeout=require_int(
                    table.get("timeout", 30), f"{path}.timeout", minimum=1
                ),
            )
        )
    return tuple(result)


def _parse_update(value: Any) -> UpdatePolicy:
    path = "update"
    table = require_table(value, path)
    reject_unknown(
        table, {"strategy", "backup", "retain_backups", "rollback_on_failure"}, path
    )
    strategy = require_string(
        table.get("strategy", "transactional"), f"{path}.strategy"
    )
    if strategy != "transactional":
        fail(f"{path}.strategy", "only transactional updates are supported")
    return UpdatePolicy(
        backup=tuple(
            safe_relative_path(item, f"{path}.backup[{index}]")
            for index, item in enumerate(
                require_array(table.get("backup", []), f"{path}.backup")
            )
        ),
        retain_backups=require_int(
            table.get("retain_backups", 3), f"{path}.retain_backups", minimum=0
        ),
        rollback_on_failure=require_bool(
            table.get("rollback_on_failure", True), f"{path}.rollback_on_failure"
        ),
    )


def parse_manifest(content: bytes, *, source: str = MANIFEST_NAME) -> Manifest:
    if len(content) > MAX_MANIFEST_SIZE:
        raise ValidationError(
            f"{source} exceeds the {MAX_MANIFEST_SIZE}-byte limit",
            path=source,
            size=len(content),
        )
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError(f"invalid {source}: {exc}", path=source) from exc

    reject_unknown(
        raw,
        {
            "manifest_version",
            "template",
            "package",
            "inputs",
            "sources",
            "files",
            "ownership",
            "runtime",
            "build",
            "checks",
            "update",
            "x",
        },
        "manifest",
    )
    require_keys(raw, {"manifest_version", "package", "runtime"}, "manifest")
    manifest_version = require_int(raw["manifest_version"], "manifest_version")
    if manifest_version != 1:
        fail("manifest_version", "unsupported schema version; expected 1")

    extensions = require_table(raw.get("x", {}), "x")
    for vendor, extension in extensions.items():
        require_table(extension, f"x.{vendor}")

    template = None
    if "template" in raw:
        template = safe_relative_path(raw["template"], "template")

    manifest = Manifest(
        manifest_version=1,
        package=_parse_package(raw["package"]),
        template=template,
        inputs=_parse_inputs(raw.get("inputs", {})),
        sources=_parse_sources(raw.get("sources", [])),
        files=_parse_files(raw.get("files", [])),
        ownership=_parse_ownership(raw.get("ownership", {})),
        runtime=_parse_runtime(raw["runtime"]),
        build=_parse_build(raw["build"]) if "build" in raw else None,
        checks=_parse_checks(raw.get("checks", [])),
        update=_parse_update(raw.get("update", {})),
        extensions=extensions,
        digest=sha256_digest(content),
    )
    _validate_secret_input_usage(manifest)
    return manifest


def _validate_secret_input_usage(manifest: Manifest) -> None:
    secret_names = {spec.name for spec in manifest.inputs if spec.secret}
    if not secret_names:
        return

    values: list[tuple[str, str]] = [
        ("runtime.image", manifest.runtime.image),
        ("runtime.workdir", manifest.runtime.workdir),
        ("runtime.stop_signal", manifest.runtime.stop_signal),
        ("runtime.restart", manifest.runtime.restart),
    ]
    if manifest.runtime.memory is not None:
        values.append(("runtime.memory", manifest.runtime.memory))
    if manifest.runtime.run_as is not None:
        values.append(("runtime.run_as", manifest.runtime.run_as))
    values.extend(
        (f"runtime.command[{index}]", value)
        for index, value in enumerate(manifest.runtime.command)
    )
    for index, mount in enumerate(manifest.runtime.mounts):
        values.extend(
            (
                (f"runtime.mounts[{index}].source", mount.source),
                (f"runtime.mounts[{index}].target", mount.target),
            )
        )
    for index, port in enumerate(manifest.runtime.ports):
        values.extend(
            (
                (f"runtime.ports[{index}].host", str(port.host)),
                (f"runtime.ports[{index}].container", str(port.container)),
            )
        )
    for check_index, check in enumerate(manifest.checks):
        for field_name, value in (
            ("path", check.path),
            ("pattern", check.pattern),
            ("host", check.host),
            ("port", check.port),
        ):
            if value is not None:
                values.append((f"checks[{check_index}].{field_name}", str(value)))
        values.extend(
            (f"checks[{check_index}].command[{argument_index}]", argument)
            for argument_index, argument in enumerate(check.command)
        )

    for path, value in values:
        referenced = set(INPUT_REFERENCE_RE.findall(value))
        leaked = sorted(referenced & secret_names)
        if leaked:
            fail(
                path,
                f"secret input {leaked[0]} may only be used in rendered files",
            )


def load_manifest(path: Path) -> Manifest:
    if path.name != MANIFEST_NAME:
        raise ValidationError(
            f"manifest must be named exactly {MANIFEST_NAME}", path=str(path)
        )
    return parse_manifest(read_bounded_utf8(path, MAX_MANIFEST_SIZE), source=str(path))
