"""Tool groups the server registers on demand.

Every tool schema a client lists is paid for in tokens on every turn, so the
server starts with the `core` set only. The model calls `load_toolset` for the
work in hand; the server registers that group's tools and sends
`notifications/tools/list_changed` so the client re-lists them.
`unload_toolset` drops a group again.

`KICAD_MCP_TOOLSETS` picks the sets loaded at startup: a comma list of set
names, or `all` (for clients that defer tool schemas themselves, such as
Claude Code with tool search). Default: `core`.

Each set is the module or package `kicad_claude.tools.<set>`, exposing
`register(mcp)`.

Every registered tool is slimmed: its listed description is the docstring
before a `Details:` line, and its input schema loses pydantic titles and the
`null` branch of optional arguments. `help(tool)` serves the full docstring.
"""

from __future__ import annotations

import asyncio
import difflib
import importlib
import inspect
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import NotificationOptions

logger = logging.getLogger("kicad-claude.toolsets")

STARTUP_ENV = "KICAD_MCP_TOOLSETS"
CORE = "core"
ALL = "all"


@dataclass(frozen=True)
class Toolset:
    name: str
    summary: str


# Order is the order `list_toolsets` reports and `all` loads.
TOOLSETS: dict[str, Toolset] = {
    t.name: t
    for t in (
        Toolset(CORE, "project open/create/state, component list, ERC, DRC, "
                "lock recovery, toolset control"),
        Toolset("sch", "schematic edit and query: symbols, wires, labels, buses, "
                "sheets, fields, nets; annotate; update PCB from schematic"),
        Toolset("pcb", "board edit and query: outline, layers, footprints, tracks, "
                "vias, zones, diff pairs, meanders, multi-board, autorouting, "
                "RF stitching"),
        Toolset("rules", "design rules, fab presets, net classes, custom DRC rules"),
        Toolset("library", "library index and search, symbol/footprint creation"),
        Toolset("review", "design review: connectivity, circuits, part ratings, "
                "power tree, what-if, design diff, lifecycle, assembly, release gate"),
        Toolset("emc_si", "EMC pre-compliance, return paths, crosstalk, ground "
                "coverage, impedance and trace-current calculators, thermal"),
        Toolset("sim", "SPICE netlist export, ngspice runs, subcircuit simulation"),
        Toolset("datasheets", "datasheet download, extraction cache, specs"),
        Toolset("sourcing", "distributor availability, vendor parts import, BOM "
                "enrichment and diff, symbol field write-back"),
        Toolset("fab", "manufacturing exports (Gerber, drill, pos, BOM, STEP, "
                "renders), fab package, panelization"),
    )
}


# A docstring line holding only this marker ends the listed description. What
# follows (arguments, outputs, method, rule IDs) is returned by `help(tool)`.
DETAILS_MARKER = "Details:"


def full_doc(tool) -> str:
    """The tool's whole docstring, dedented."""
    return inspect.cleandoc(tool.fn.__doc__ or tool.description or "")


def summary_of(doc: str) -> str:
    """The part of `doc` before the `Details:` line (all of it without one)."""
    head, sep, _ = doc.partition(f"\n{DETAILS_MARKER}\n")
    if not sep and doc.endswith(f"\n{DETAILS_MARKER}"):
        head = doc[: -len(DETAILS_MARKER) - 1]
    return head.strip()


def _compact_schema(node: Any) -> Any:
    """Strip what the listed schema carries for nothing, paid in tokens.

    - pydantic's generated `title` on every model and field;
    - `X | None = None`: `anyOf [X, {"type": "null"}]` + `"default": null`
      becomes plain X. Omitting the argument still means None — arguments
      are validated against the tool's pydantic model, not this schema.
    """
    if isinstance(node, list):
        return [_compact_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _compact_schema(v) for name, v in value.items()}
        else:
            out[key] = _compact_schema(value)
    branches = out.get("anyOf")
    if isinstance(branches, list) and len(branches) == 2 and {"type": "null"} in branches:
        (kept,) = [b for b in branches if b != {"type": "null"}]
        del out["anyOf"]
        if out.get("default", ...) is None:
            del out["default"]
        out = {**kept, **out}
    return out


def slim(tool) -> None:
    """List `tool` with its summary only and a compact input schema."""
    tool.description = summary_of(full_doc(tool))
    tool.parameters = _compact_schema(tool.parameters)


def _register_set(mcp, toolset: Toolset) -> list[str]:
    """Register the set on `mcp`, slim the new tools, return their names."""
    before = _tool_names(mcp)
    importlib.import_module(f"kicad_claude.tools.{toolset.name}").register(mcp)
    added = sorted(_tool_names(mcp) - before)
    for name in added:
        slim(mcp._tool_manager.get_tool(name))
    return added


def _tool_names(mcp) -> set[str]:
    return {t.name for t in mcp._tool_manager.list_tools()}


_catalog: dict[str, list[str]] | None = None
_docs: dict[str, str] = {}


def catalog() -> dict[str, list[str]]:
    """Tool names of every set, loaded or not. Built once on a scratch server."""
    global _catalog
    if _catalog is None:
        from mcp.server.fastmcp import FastMCP

        out: dict[str, list[str]] = {}
        for ts in TOOLSETS.values():
            scratch = FastMCP("catalog")
            out[ts.name] = _register_set(scratch, ts)
            for name in out[ts.name]:
                _docs[name] = full_doc(scratch._tool_manager.get_tool(name))
        _catalog = out
    return _catalog


def tool_doc(name: str) -> str | None:
    """Full docstring of any set's tool, loaded or not."""
    catalog()
    return _docs.get(name)


def owner_of(tool_name: str) -> str | None:
    """The set a tool belongs to, or None for a name no set has."""
    for name, tools in catalog().items():
        if tool_name in tools:
            return name
    return None


def parse_names(raw: str | list[str] | None) -> list[str]:
    """Normalize a comma string or list of set names; expand `all`."""
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    names = [s.strip() for s in items if s and s.strip()]
    if ALL in names:
        return list(TOOLSETS)
    unknown = [n for n in names if n not in TOOLSETS]
    if unknown:
        raise ValueError(
            f"unknown toolset(s) {', '.join(map(repr, unknown))}. "
            f"Known: {', '.join(TOOLSETS)}, {ALL}"
        )
    return names


class ToolsetManager:
    """Registers and removes tool sets on one FastMCP server."""

    def __init__(self, mcp) -> None:
        self.mcp = mcp
        self.loaded: dict[str, list[str]] = {}

    def load(self, name: str) -> list[str]:
        """Register set `name`. Returns the tool names added ([] if loaded)."""
        if name in self.loaded:
            return []
        added = _register_set(self.mcp, TOOLSETS[name])
        self.loaded[name] = added
        logger.info("toolset %s loaded: %d tools", name, len(added))
        return added

    def unload(self, name: str) -> list[str]:
        """Remove set `name`. Returns the tool names removed ([] if not loaded)."""
        if name == CORE:
            raise ValueError("the core toolset cannot be unloaded")
        removed = self.loaded.pop(name, [])
        for tool in removed:
            self.mcp.remove_tool(tool)
        logger.info("toolset %s unloaded: %d tools", name, len(removed))
        return removed

    def status(self) -> list[dict[str, Any]]:
        cat = catalog()
        return [
            {"name": ts.name, "summary": ts.summary, "loaded": ts.name in self.loaded,
             "tools": self.loaded.get(ts.name) or cat[ts.name]}
            for ts in TOOLSETS.values()
        ]


async def _notify_list_changed(ctx: Context | None) -> bool:
    """Tell the client to re-list tools. False when there is no session."""
    if ctx is None:
        return False
    try:
        await ctx.session.send_tool_list_changed()
    except Exception as exc:  # noqa: BLE001 — no live request outside a session
        logger.debug("tools/list_changed not sent: %s", exc)
        return False
    return True


def _sets_line() -> str:
    return "; ".join(f"{ts.name}: {ts.summary}" for ts in TOOLSETS.values()
                     if ts.name != CORE)


def _install_unloaded_hint(manager: ToolsetManager) -> None:
    """A call to a tool of an unloaded set names the set to load."""
    tm = manager.mcp._tool_manager
    original = tm.call_tool

    async def call_tool(name: str, arguments: dict[str, Any], *args, **kwargs):
        if tm.get_tool(name) is None:
            owner = owner_of(name)
            if owner is not None:
                raise ToolError(
                    f"{name} is in toolset {owner!r}, which is not loaded. "
                    f"Call load_toolset(toolsets=[{owner!r}]) first."
                )
        return await original(name, arguments, *args, **kwargs)

    tm.call_tool = call_tool


# One tool runs at a time: tools share the active-project state and write the
# same files. A threading lock, held inside the worker thread, keeps that true
# even when a cancelled call's thread is still finishing.
_TOOL_LOCK = threading.Lock()


def _run_sync_tools_off_loop(mcp) -> None:
    """Run plain-`def` tools in a worker thread.

    FastMCP calls them on the event loop, so a kicad-cli or pcbnew run of
    several minutes froze the whole server: no ping, no tool list, no
    cancellation. The call (argument validation included) now runs on its own
    event loop in a thread, one tool at a time.
    """
    tm = mcp._tool_manager
    original = tm.call_tool

    async def call_tool(name: str, arguments: dict[str, Any], *args, **kwargs):
        tool = tm.get_tool(name)
        if tool is None or tool.is_async:
            return await original(name, arguments, *args, **kwargs)

        def work():
            with _TOOL_LOCK:
                return asyncio.run(original(name, arguments, *args, **kwargs))

        return await anyio.to_thread.run_sync(work)

    tm.call_tool = call_tool


def _advertise_list_changed(mcp) -> None:
    """Declare `tools.listChanged` — FastMCP does not by default."""
    low = mcp._mcp_server
    original = low.create_initialization_options

    def create_initialization_options(notification_options=None, *args, **kwargs):
        if notification_options is None:
            notification_options = NotificationOptions(tools_changed=True)
        return original(notification_options, *args, **kwargs)

    low.create_initialization_options = create_initialization_options


def install(mcp, startup: str | list[str] | None = None) -> ToolsetManager:
    """Register core, the control tools, and the startup sets on `mcp`.

    `startup` defaults to `$KICAD_MCP_TOOLSETS`, else `core`.
    """
    manager = ToolsetManager(mcp)
    if startup is None:
        startup = os.environ.get(STARTUP_ENV) or CORE
    names = parse_names(startup)

    manager.load(CORE)

    @mcp.tool(description=(
        "Load tool groups for the work in hand; their tools appear right after. "
        "Pass several at once, or 'all'. Sheet-level tools take an optional `sheet` "
        "(root, file or sheet name), board-level ones `board` (main or a .kicad_pcb), "
        "else the active one. Sets: " + _sets_line() + "."
    ))
    async def load_toolset(toolsets: list[str], ctx: Context) -> dict:
        added = {n: manager.load(n) for n in parse_names(toolsets)}
        notified = await _notify_list_changed(ctx) if any(added.values()) else False
        return {
            "added": {n: len(tools) for n, tools in added.items() if tools},
            "already_loaded": [n for n, tools in added.items() if not tools],
            "loaded": list(manager.loaded),
            "client_notified": notified,
        }

    @mcp.tool()
    async def unload_toolset(toolsets: list[str], ctx: Context) -> dict:
        """Remove tool groups no longer needed, to save context. `core` stays."""
        names = [n for n in parse_names(toolsets) if n != CORE]
        removed = {n: manager.unload(n) for n in names}
        notified = await _notify_list_changed(ctx) if any(removed.values()) else False
        return {
            "removed": {n: len(tools) for n, tools in removed.items() if tools},
            "loaded": list(manager.loaded),
            "client_notified": notified,
        }

    @mcp.tool()
    def list_toolsets() -> dict:
        """Every tool group: summary, whether loaded, and its tool names."""
        return {"toolsets": manager.status()}

    @mcp.tool(name="help")
    def tool_help(tool: str) -> str:
        """Full docs of any tool, loaded or not: arguments, outputs, method, rule IDs.

        Listed descriptions are summaries. Read this before the first call of a
        review or analysis tool, or when an argument's meaning is unclear.
        """
        live = mcp._tool_manager.get_tool(tool)
        doc = full_doc(live) if live is not None else tool_doc(tool)
        if doc is None:
            close = difflib.get_close_matches(tool, sorted(_docs), n=3)
            raise ValueError(f"no tool {tool!r}" + (f"; did you mean {', '.join(close)}?"
                                                    if close else ""))
        owner = owner_of(tool)
        loaded = live is not None
        head = f"{tool} (toolset {owner or CORE}{'' if loaded else ', not loaded'})"
        return f"{head}\n\n{doc}"

    manager.loaded[CORE] += ["help", "list_toolsets", "load_toolset", "unload_toolset"]
    for tool in mcp._tool_manager.list_tools():  # ping and the control tools
        slim(tool)
    for name in names:
        manager.load(name)

    _run_sync_tools_off_loop(mcp)
    _install_unloaded_hint(manager)
    _advertise_list_changed(mcp)
    return manager
