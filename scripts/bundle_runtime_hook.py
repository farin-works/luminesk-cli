"""Private frozen-only import probe; ordinary CLI startup stays lazy."""

import sys

if sys.argv[1:] == ["--_bundle-check"]:
    import importlib

    for module in (
        "defusedxml.ElementTree",
        "filelock",
        "httpx",
        "platformdirs",
        "prompt_toolkit",
        "prompt_toolkit.input.defaults",
        "prompt_toolkit.output.defaults",
        "rich",
        "luminesk_cli.cli.entry",
        "luminesk_cli.cli.attach_tui",
    ):
        importlib.import_module(module)
    print("Luminesk bundle imports OK (including prompt_toolkit)")
    raise SystemExit(0)
