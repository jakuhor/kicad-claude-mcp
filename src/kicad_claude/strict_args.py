"""Reject tool calls that carry keyword arguments the tool does not have.

FastMCP validates arguments with a pydantic model whose `extra` policy is
`ignore`, so `set_design_rules(min_clearance=0.15)` — the `_mm` suffix left
off — returns success and changes nothing. The call reads as confirmation
because the response echoes the current rules. Making the unknown name an
error is the whole point: a silent no-op on a design tool is worse than a
failed call.

Deprecated / commonly guessed spellings are accepted as **aliases** and
renamed to the tool's canonical argument before validation; the response then
carries `deprecated_arguments` naming the canonical spelling (open_points_3
Q12). An alias is only applied when the tool lacks the alias name and has the
target, and never when the caller passed both spellings.

`install(mcp)` wraps the tool manager once, so every tool group gets the check
without touching a single tool signature.
"""

from __future__ import annotations

import difflib
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

# alias -> canonical, applied to any tool that has the canonical name and not
# the alias. The canonical net argument is `net`; PCB tools that still take
# `net_name` accept `net` the other way round.
GENERIC_ALIASES: dict[str, str] = {
    "net_name": "net",
    "net": "net_name",
    "x": "x_mm",
    "y": "y_mm",
}

# Per-tool aliases: renamed arguments and spellings seen in real use.
TOOL_ALIASES: dict[str, dict[str, str]] = {
    "set_project": {"path": "project_path"},
    "add_label": {"text": "net", "name": "net"},
    "add_hierarchical_label": {"text": "net", "name": "net"},
    "add_wire": {
        "start_x_mm": "x1_mm", "start_y_mm": "y1_mm",
        "end_x_mm": "x2_mm", "end_y_mm": "y2_mm",
        "start_x": "x1_mm", "start_y": "y1_mm", "end_x": "x2_mm", "end_y": "y2_mm",
    },
    "replace_symbol": {"new_lib_id": "lib_id"},
}


def _message(name: str, unknown: list[str], accepted: set[str]) -> str:
    parts = []
    for arg in unknown:
        close = difflib.get_close_matches(arg, sorted(accepted), n=2, cutoff=0.6)
        parts.append(f"{arg!r}" + (f" (did you mean {' or '.join(close)}?)" if close else ""))
    return (
        f"{name}: unknown argument(s) {', '.join(parts)}. "
        f"Accepted: {', '.join(sorted(accepted)) or '(none)'}"
    )


def resolve_aliases(
    name: str, arguments: dict[str, Any], accepted: set[str]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Rename alias arguments to canonical ones. Returns (arguments, renames)."""
    table = {**GENERIC_ALIASES, **TOOL_ALIASES.get(name, {})}
    out = dict(arguments)
    renames: list[dict[str, str]] = []
    for alias, canonical in table.items():
        if alias not in arguments or alias in accepted or canonical not in accepted:
            continue
        if canonical in arguments:
            raise ToolError(
                f"{name}: got both {alias!r} and {canonical!r}; pass only {canonical!r}"
            )
        out[canonical] = out.pop(alias)
        renames.append({"given": alias, "use": canonical})
    return out, renames


def install(mcp) -> None:
    """Make every tool on `mcp` reject unknown keyword arguments."""
    manager = mcp._tool_manager
    if getattr(manager, "_strict_args_installed", False):
        return
    original = manager.call_tool

    async def call_tool(name: str, arguments: dict[str, Any], *args, **kwargs):
        tool = manager.get_tool(name)
        renames: list[dict[str, str]] = []
        if tool is not None and isinstance(arguments, dict):
            accepted = set(tool.fn_metadata.arg_model.model_fields)
            arguments, renames = resolve_aliases(name, arguments, accepted)
            unknown = sorted(set(arguments) - accepted)
            if unknown:
                raise ToolError(_message(name, unknown, accepted))
        if not renames:
            return await original(name, arguments, *args, **kwargs)

        # Run unconverted so the notice can go into the tool's own dict result.
        convert = kwargs.pop("convert_result", False)
        if len(args) >= 2:  # (context, convert_result) passed positionally
            convert = args[1]
            args = args[:1]
        result = await original(name, arguments, *args, convert_result=False, **kwargs)
        if isinstance(result, dict):
            result = {**result, "deprecated_arguments": renames}
        return tool.fn_metadata.convert_result(result) if convert else result

    manager.call_tool = call_tool
    manager._strict_args_installed = True
