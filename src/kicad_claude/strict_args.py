"""Reject tool calls that carry keyword arguments the tool does not have.

FastMCP validates arguments with a pydantic model whose `extra` policy is
`ignore`, so `set_design_rules(min_clearance=0.15)` — the `_mm` suffix left
off — returns success and changes nothing. The call reads as confirmation
because the response echoes the current rules. Making the unknown name an
error is the whole point: a silent no-op on a design tool is worse than a
failed call.

`install(mcp)` wraps the tool manager once, so every tool group gets the check
without touching a single tool signature.
"""

from __future__ import annotations

import difflib
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError


def _message(name: str, unknown: list[str], accepted: set[str]) -> str:
    parts = []
    for arg in unknown:
        close = difflib.get_close_matches(arg, sorted(accepted), n=2, cutoff=0.6)
        parts.append(f"{arg!r}" + (f" (did you mean {' or '.join(close)}?)" if close else ""))
    return (
        f"{name}: unknown argument(s) {', '.join(parts)}. "
        f"Accepted: {', '.join(sorted(accepted)) or '(none)'}"
    )


def install(mcp) -> None:
    """Make every tool on `mcp` reject unknown keyword arguments."""
    manager = mcp._tool_manager
    if getattr(manager, "_strict_args_installed", False):
        return
    original = manager.call_tool

    async def call_tool(name: str, arguments: dict[str, Any], *args, **kwargs):
        tool = manager.get_tool(name)
        if tool is not None and isinstance(arguments, dict):
            accepted = set(tool.fn_metadata.arg_model.model_fields)
            unknown = sorted(set(arguments) - accepted)
            if unknown:
                raise ToolError(_message(name, unknown, accepted))
        return await original(name, arguments, *args, **kwargs)

    manager.call_tool = call_tool
    manager._strict_args_installed = True
