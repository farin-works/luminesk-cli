"""Standard-library-only command metadata and argument parser."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import NoReturn


@dataclass(slots=True, frozen=True)
class ParsedCommand:
    handler: str
    namespace: argparse.Namespace


class LumineskArgumentParser(argparse.ArgumentParser):
    """Argument parser with the same stable JSON error envelope as commands."""

    json_errors = False

    def error(self, message: str) -> NoReturn:
        usage = self.format_usage().strip()
        if self.json_errors:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "usage",
                            "message": message,
                            "details": {"usage": usage},
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            from luminesk_cli.cli.output import print_usage_error

            self.print_usage(sys.stderr)
            print_usage_error(self.prog, message)
        self.exit(2)


def _automation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit stable JSON output.")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable prompts; required choices become errors.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit diagnostic stage logs to stderr.",
    )


def _validation_levels(parser: argparse.ArgumentParser) -> None:
    levels = parser.add_mutually_exclusive_group()
    levels.add_argument("--static", action="store_true")
    levels.add_argument("--resolve", action="store_true")
    levels.add_argument("--build", action="store_true")
    levels.add_argument("--instance", action="store_true")
    levels.add_argument("--readiness", action="store_true")
    levels.add_argument("--all", action="store_true")


def _input_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Set a non-secret recipe input.",
    )
    parser.add_argument(
        "--set-file",
        action="append",
        default=[],
        metavar="KEY=PATH",
        help="Read a recipe input from a UTF-8 file; required for secret inputs.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = LumineskArgumentParser(
        prog="nesk",
        description="Luminesk server and server-template composer.",
    )
    parser.add_argument("--version", "-v", action="store_true", help="Show version.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Emit diagnostic stage logs to stderr.",
    )
    commands = parser.add_subparsers(dest="command", required=False)

    init_parser = commands.add_parser("init", help="Create a recipe skeleton.")
    init_parser.add_argument("--dir", default=".", help="Recipe directory.")
    init_parser.add_argument("--name", default=None, help="Package name.")
    _automation_options(init_parser)
    init_parser.set_defaults(handler="luminesk_cli.cli.commands.init:run")

    validate = commands.add_parser("validate", help="Validate a recipe or instance.")
    validate.add_argument("--dir", default=".", help="Recipe or instance directory.")
    _validation_levels(validate)
    _input_options(validate)
    _automation_options(validate)
    validate.set_defaults(handler="luminesk_cli.cli.commands.validate:run")

    lock = commands.add_parser("lock", help="Resolve sources and write luminesk.lock.")
    lock.add_argument("--dir", default=".", help="Recipe directory.")
    lock.add_argument("--frozen", action="store_true", help="Use lock and cache only.")
    _automation_options(lock)
    lock.set_defaults(handler="luminesk_cli.cli.commands.lock:run")

    plan = commands.add_parser(
        "plan", help="Show install/update changes without writes."
    )
    plan.add_argument("--dir", default=".", help="Instance directory.")
    plan.add_argument(
        "--frozen", action="store_true", help="Use the existing lock offline."
    )
    _input_options(plan)
    _automation_options(plan)
    plan.set_defaults(handler="luminesk_cli.cli.commands.plan:run")

    install = commands.add_parser(
        "install",
        aliases=["i"],
        help="Install an official, local, or GitHub recipe transactionally.",
    )
    install.add_argument(
        "source",
        nargs="?",
        help="Official NAME, OWNER/REPO, HTTPS GitHub URL, or local recipe.",
    )
    install.add_argument("--dir", default=None, help="Target instance directory.")
    install.add_argument("--ref", default=None, help="Git branch, tag, or commit.")
    _input_options(install)
    install.add_argument("--dry-run", action="store_true")
    install.add_argument(
        "--frozen", action="store_true", help="Use lock and cache only."
    )
    install.add_argument(
        "--yes", action="store_true", help="Accept the displayed trust plan."
    )
    _automation_options(install)
    install.set_defaults(handler="luminesk_cli.cli.commands.install:run")

    update = commands.add_parser(
        "update",
        help="Resolve and apply updates transactionally.",
    )
    update.add_argument("component", nargs="?", help="Optional source id to update.")
    update.add_argument("--dir", default=None, help="Instance directory.")
    _input_options(update)
    update.add_argument("--dry-run", action="store_true")
    update.add_argument(
        "--frozen", action="store_true", help="Use lock and cache only."
    )
    update.add_argument("--yes", action="store_true")
    _automation_options(update)
    update.set_defaults(handler="luminesk_cli.cli.commands.update:run")

    outdated = commands.add_parser("outdated", help="Show available updates.")
    outdated.add_argument("--dir", default=None, help="Instance directory.")
    _automation_options(outdated)
    outdated.set_defaults(handler="luminesk_cli.cli.commands.update:outdated")

    diff = commands.add_parser("diff", help="Show recipe and managed-file drift.")
    diff.add_argument("--dir", default=None, help="Instance directory.")
    diff.add_argument(
        "--offline",
        action="store_true",
        help="Inspect only local drift without fetching the upstream recipe.",
    )
    _automation_options(diff)
    diff.set_defaults(handler="luminesk_cli.cli.commands.update:diff")

    recover = commands.add_parser(
        "recover", help="Roll back an interrupted transaction."
    )
    recover.add_argument("--dir", default=None, help="Instance directory.")
    recover.add_argument(
        "--force-clean",
        action="store_true",
        help="Restore the newest retained backup without an active journal.",
    )
    recover.add_argument("--yes", action="store_true", help="Confirm forced recovery.")
    _automation_options(recover)
    recover.set_defaults(handler="luminesk_cli.cli.commands.update:recover")

    cache = commands.add_parser("cache", help="Inspect the content-addressed cache.")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_verify = cache_commands.add_parser("verify", help="Verify every cached blob.")
    _automation_options(cache_verify)
    cache_verify.set_defaults(handler="luminesk_cli.cli.commands.cache:verify")
    cache_prune = cache_commands.add_parser("prune", help="Remove old cached blobs.")
    cache_prune.add_argument(
        "--max-age",
        type=int,
        default=30,
        metavar="DAYS",
        help="Remove blobs at least this many days old.",
    )
    cache_prune.add_argument("--dry-run", action="store_true")
    _automation_options(cache_prune)
    cache_prune.set_defaults(handler="luminesk_cli.cli.commands.cache:prune")

    import_parser = commands.add_parser(
        "import", help="Rebuild the global index from local instance state."
    )
    import_parser.add_argument("path", help="Instance path or scan root.")
    import_parser.add_argument("--scan", action="store_true")
    _automation_options(import_parser)
    import_parser.set_defaults(
        handler="luminesk_cli.cli.commands.instance_index:import_instances"
    )

    list_parser = commands.add_parser(
        "list", help="List installed instances from the local index."
    )
    _automation_options(list_parser)
    list_parser.set_defaults(
        handler="luminesk_cli.cli.commands.instance_index:list_instances"
    )

    search = commands.add_parser(
        "search",
        help="Search the verified official recipe catalog offline.",
    )
    search.add_argument("query", nargs="?")
    search.add_argument("--type", choices=["core", "template"], default=None)
    search.add_argument(
        "--edition", choices=["java", "bedrock", "cross-platform"], default=None
    )
    search.add_argument(
        "--limit", type=int, default=50, help="Maximum results to return (default: 50)."
    )
    search.add_argument("--all", action="store_true", help="Return every match.")
    _automation_options(search)
    search.set_defaults(handler="luminesk_cli.cli.commands.catalog:search")

    info = commands.add_parser("info", help="Show one catalog recipe.")
    info.add_argument("name", help="Official database entry name.")
    _automation_options(info)
    info.set_defaults(handler="luminesk_cli.cli.commands.catalog:info")

    catalog = commands.add_parser(
        "catalog",
        help="Manage the verified official catalog snapshot.",
    )
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_update = catalog_commands.add_parser(
        "update", help="Download and verify the latest official snapshot."
    )
    _automation_options(catalog_update)
    catalog_update.set_defaults(handler="luminesk_cli.cli.commands.catalog:update")
    catalog_status = catalog_commands.add_parser(
        "status", help="Show the active catalog snapshot."
    )
    _automation_options(catalog_status)
    catalog_status.set_defaults(handler="luminesk_cli.cli.commands.catalog:status")
    catalog_verify = catalog_commands.add_parser(
        "verify", help="Verify the active catalog snapshot."
    )
    _automation_options(catalog_verify)
    catalog_verify.set_defaults(handler="luminesk_cli.cli.commands.catalog:verify")
    catalog_use = catalog_commands.add_parser(
        "use", help="Activate a previously cached exact snapshot."
    )
    catalog_use.add_argument("revision", help="Exact cached database commit SHA.")
    _automation_options(catalog_use)
    catalog_use.set_defaults(handler="luminesk_cli.cli.commands.catalog:use")

    doctor = commands.add_parser(
        "doctor",
        help="Check required and optional tools.",
    )
    _automation_options(doctor)
    doctor.set_defaults(handler="luminesk_cli.cli.commands.doctor:run")

    start = commands.add_parser("start", help="Start the current recipe instance.")
    start.add_argument("--dir", default=None, help="Instance directory.")
    _input_options(start)
    start.add_argument("--no-wait", action="store_true", help="Skip readiness checks.")
    start.add_argument(
        "--attach",
        action="store_true",
        help="Open the interactive console after start (also for a running server).",
    )
    _automation_options(start)
    start.set_defaults(handler="luminesk_cli.cli.commands.runtime:start")

    stop = commands.add_parser("stop", help="Stop the current recipe instance.")
    stop.add_argument("--dir", default=None, help="Instance directory.")
    _automation_options(stop)
    stop.set_defaults(handler="luminesk_cli.cli.commands.runtime:stop")

    restart = commands.add_parser(
        "restart", help="Restart the current recipe instance."
    )
    restart.add_argument("--dir", default=None, help="Instance directory.")
    _input_options(restart)
    restart.add_argument("--no-wait", action="store_true")
    _automation_options(restart)
    restart.set_defaults(handler="luminesk_cli.cli.commands.runtime:restart")

    status = commands.add_parser("status", help="Show current instance runtime status.")
    status.add_argument("--dir", default=None, help="Instance directory.")
    _automation_options(status)
    status.set_defaults(handler="luminesk_cli.cli.commands.runtime:status")

    logs = commands.add_parser("logs", help="Read current instance Docker logs.")
    logs.add_argument("--dir", default=None, help="Instance directory.")
    logs.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="Continue streaming new log output.",
    )
    logs.add_argument(
        "--tail",
        type=int,
        default=200,
        metavar="LINES",
        help="Start with the latest LINES (default: 200; maximum: 100000).",
    )
    logs.add_argument(
        "--since",
        default=None,
        metavar="TIME",
        help="Show logs since a Docker duration or timestamp.",
    )
    logs.add_argument(
        "--timestamps",
        "-t",
        action="store_true",
        help="Include Docker timestamps.",
    )
    _automation_options(logs)
    logs.set_defaults(handler="luminesk_cli.cli.commands.runtime:logs")

    attach = commands.add_parser(
        "attach", help="Open the current instance console TUI."
    )
    attach.add_argument("--dir", default=None, help="Instance directory.")
    _automation_options(attach)
    attach.set_defaults(handler="luminesk_cli.cli.commands.runtime:attach")

    return parser


def parse_command(argv: list[str]) -> ParsedCommand:
    parser = build_parser()
    _set_json_errors(parser, "--json" in argv)
    namespace = parser.parse_args(argv)

    if namespace.version:
        from luminesk_cli._version import __version__

        parser.exit(message=f"Luminesk {__version__}\n")

    handler = getattr(namespace, "handler", None)

    if handler is None:
        parser.print_help()
        return ParsedCommand("luminesk_cli.cli.commands.noop:run", namespace)

    return ParsedCommand(handler=handler, namespace=namespace)


def _set_json_errors(parser: argparse.ArgumentParser, enabled: bool) -> None:
    if isinstance(parser, LumineskArgumentParser):
        parser.json_errors = enabled
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in set(action.choices.values()):
                _set_json_errors(child, enabled)
