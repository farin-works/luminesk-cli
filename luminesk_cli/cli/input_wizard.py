"""Interactive recipe-input collection for human install sessions."""

from __future__ import annotations

import json
from collections.abc import Mapping

from luminesk_cli.cli.commands.common import parse_inputs
from luminesk_cli.cli.output import ask, print_error, print_human
from luminesk_cli.domain.errors import ConflictError, ValidationError
from luminesk_cli.domain.inputs import InputValue, resolve_inputs
from luminesk_cli.domain.manifest import InputSpec, Manifest

BOOLEAN_VALUES = {
    "false": "false",
    "n": "false",
    "no": "false",
    "true": "true",
    "y": "true",
    "yes": "true",
}


def collect_install_inputs(
    manifest: Manifest,
    arguments: list[str],
    file_arguments: list[str],
    *,
    interactive: bool,
) -> dict[str, InputValue]:
    """Parse explicit overrides and prompt for the rest when permitted."""

    return collect_recipe_inputs(
        manifest,
        arguments,
        file_arguments,
        interactive=interactive,
    )


def collect_recipe_inputs(
    manifest: Manifest,
    arguments: list[str],
    file_arguments: list[str],
    *,
    interactive: bool,
    known_values: Mapping[str, InputValue] | None = None,
) -> dict[str, InputValue]:
    """Merge known values and overrides, prompting for undeclared values."""

    declared = {spec.name for spec in manifest.inputs}
    values = {
        name: value for name, value in (known_values or {}).items() if name in declared
    }
    values.update(parse_inputs(manifest, arguments, file_arguments))
    resolve_inputs(manifest, values, require_required=False)

    pending = [spec for spec in manifest.inputs if spec.name not in values]
    if not interactive or not pending:
        resolve_inputs(manifest, values)
        return values

    print_human(
        f"Configure inputs for {manifest.package.display_name or manifest.package.name}\n"
        "Press Enter to keep a default or skip an optional input.",
        tone="info",
    )
    for index, spec in enumerate(pending, start=1):
        print_human(f"Input {index} of {len(pending)}", tone="info")
        values.update(_prompt_for_input(manifest, spec, values))

    resolve_inputs(manifest, values)
    return values


def _prompt_for_input(
    manifest: Manifest,
    spec: InputSpec,
    values: dict[str, InputValue],
) -> dict[str, InputValue]:
    print_human(_input_description(spec), tone="heading")

    while True:
        try:
            raw_value = ask(_input_question(spec)).strip()
        except EOFError as exc:
            raise ConflictError(
                "recipe input collection was not completed; use --non-interactive "
                "with explicit --set/--set-file values for automation"
            ) from exc

        if not raw_value:
            if spec.default is not None or not spec.required:
                return {}
            print_error("validation", f"input {spec.name} is required")
            continue

        if spec.type == "boolean" and not spec.secret:
            normalized = BOOLEAN_VALUES.get(raw_value.casefold())
            if normalized is None:
                print_error(
                    "validation",
                    f"input {spec.name} must be true/false or yes/no",
                )
                continue
            raw_value = normalized

        try:
            parsed = (
                parse_inputs(manifest, [], [f"{spec.name}={raw_value}"])
                if spec.secret
                else parse_inputs(manifest, [f"{spec.name}={raw_value}"], [])
            )
            resolve_inputs(
                manifest,
                {**values, **parsed},
                require_required=False,
            )
        except ValidationError as exc:
            print_error("validation", exc.message)
            continue
        return parsed


def _input_description(spec: InputSpec) -> str:
    lines = [spec.prompt or f"Configure {spec.name}"]
    attributes: list[str] = [spec.type]
    attributes.append("required" if spec.required else "optional")
    if spec.secret:
        attributes.append("secret file")
    if spec.default is None:
        attributes.append("no default")
    else:
        attributes.append(f"default {json.dumps(spec.default, ensure_ascii=False)}")
    lines.append(f"  {spec.name}: " + ", ".join(attributes))

    constraints = []
    if spec.minimum is not None:
        constraints.append(f"minimum {spec.minimum}")
    if spec.maximum is not None:
        constraints.append(f"maximum {spec.maximum}")
    if spec.pattern is not None:
        constraints.append(f"pattern {spec.pattern}")
    if constraints:
        lines.append("  Constraints: " + ", ".join(constraints))
    return "\n".join(lines)


def _input_question(spec: InputSpec) -> str:
    if spec.secret:
        return f"Path for {spec.name}:"
    if spec.type == "boolean":
        return f"{spec.name} [true/false]:"
    return f"{spec.name}:"
