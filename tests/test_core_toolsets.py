"""Tool sets loaded on demand (`kicad_claude/toolsets.py`)."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from kicad_claude import strict_args, toolsets

CONTROL = {"help", "list_toolsets", "load_toolset", "unload_toolset"}

# Token budgets. Every listed tool is paid for in context on every turn, so a
# new tool or argument must fit. Over a cap: shorten the summary and move the
# rest below a `Details:` line in the docstring (`help(tool)` returns it),
# rather than raising the cap. Sizes are characters of the listed JSON
# (name + description + inputSchema); ~4 characters per token.
DESCRIPTION_MAX = 500
MENU_DESCRIPTION_MAX = 1300  # load_toolset: one line per set
TOOL_MAX = 1600
SET_BUDGET = {  # ~5-10 % above the sizes after the v2.0 merges (2026-11-11)
    "core": 5000, "sch": 22500, "pcb": 18700, "rules": 5500, "library": 4000,
    # review +505: OP-17 A7 derating_profile x2, A3 i2c_mode / i2c_bus_capacitance_pf,
    # A5 source_ohm / inrush_limit_a (measured +481, + 5 %)
    "review": 9805,
    "emc_si": 8940,  # +1140: OP-17 B3 analyze_board_thermal (measured 1085, + 5 %)
    "sim": 2800, "datasheets": 3300,
    "sourcing": 5800, "fab": 2600,
}


def _server(startup="core"):
    mcp = FastMCP("t")
    manager = toolsets.install(mcp, startup=startup)
    strict_args.install(mcp)
    return mcp, manager


def _names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _call(mcp, name, args, context=None):
    return asyncio.run(mcp._tool_manager.call_tool(name, args, context=context))


class _Session:
    def __init__(self):
        self.sent = 0

    async def send_tool_list_changed(self):
        self.sent += 1


class _Ctx:
    def __init__(self):
        self.session = _Session()


def test_startup_registers_core_only():
    mcp, _ = _server()
    names = _names(mcp)
    assert CONTROL <= names
    assert set(toolsets.catalog()["core"]) <= names
    assert "add_wire" not in names
    assert len(names) == len(toolsets.catalog()["core"]) + len(CONTROL)


def test_every_tool_in_exactly_one_set():
    cat = toolsets.catalog()
    seen: dict[str, str] = {}
    for name, tools in cat.items():
        assert tools, f"toolset {name} registers no tools"
        for t in tools:
            assert t not in seen, f"{t} in both {seen[t]} and {name}"
            seen[t] = name
    assert not CONTROL & set(seen)


def test_all_loads_every_set():
    mcp, manager = _server("all")
    assert list(manager.loaded) == list(toolsets.TOOLSETS)
    expected = set().union(*map(set, toolsets.catalog().values())) | CONTROL
    assert _names(mcp) == expected


def test_load_adds_tools_and_notifies_client():
    mcp, _ = _server()
    ctx = _Ctx()
    out = _call(mcp, "load_toolset", {"toolsets": ["sch", "rules"]}, context=ctx)
    assert out["added"] == {"sch": len(toolsets.catalog()["sch"]),
                            "rules": len(toolsets.catalog()["rules"])}
    assert out["client_notified"] is True and ctx.session.sent == 1
    assert {"add_wire", "set_design_rules"} <= _names(mcp)


def test_load_is_idempotent():
    mcp, _ = _server()
    _call(mcp, "load_toolset", {"toolsets": ["pcb"]})
    ctx = _Ctx()
    out = _call(mcp, "load_toolset", {"toolsets": ["pcb"]}, context=ctx)
    assert out["added"] == {} and out["already_loaded"] == ["pcb"]
    assert ctx.session.sent == 0  # nothing changed, nothing to re-list


def test_unload_removes_tools_and_reload_works():
    mcp, _ = _server("core,sch")
    ctx = _Ctx()
    out = _call(mcp, "unload_toolset", {"toolsets": ["sch"]}, context=ctx)
    assert out["removed"] == {"sch": len(toolsets.catalog()["sch"])}
    assert ctx.session.sent == 1
    assert "add_wire" not in _names(mcp)
    _call(mcp, "load_toolset", {"toolsets": ["sch"]})
    assert "add_wire" in _names(mcp)


def test_core_cannot_be_unloaded():
    mcp, _ = _server()
    out = _call(mcp, "unload_toolset", {"toolsets": ["core"]})
    assert out["removed"] == {}
    assert "run_drc" in _names(mcp)


def test_unknown_toolset_is_rejected():
    mcp, _ = _server()
    with pytest.raises(ToolError, match="unknown toolset"):
        _call(mcp, "load_toolset", {"toolsets": ["pcbx"]})
    with pytest.raises(ValueError, match="unknown toolset"):
        _server("core,nope")


def test_call_to_unloaded_tool_names_its_set():
    mcp, _ = _server()
    with pytest.raises(ToolError, match=r"toolset 'pcb'.*load_toolset"):
        _call(mcp, "add_track", {})


def test_unknown_tool_still_reports_unknown():
    mcp, _ = _server()
    with pytest.raises(ToolError, match="Unknown tool"):
        _call(mcp, "no_such_tool", {})


def test_strict_args_applies_to_loaded_sets():
    mcp, _ = _server("core,rules")
    with pytest.raises(ToolError, match="min_clearance_mm"):
        _call(mcp, "set_design_rules", {"min_clearance": 0.15})


def test_list_toolsets_reports_state():
    mcp, _ = _server("core,fab")
    rows = {r["name"]: r for r in _call(mcp, "list_toolsets", {})["toolsets"]}
    assert set(rows) == set(toolsets.TOOLSETS)
    assert rows["fab"]["loaded"] and not rows["sch"]["loaded"]
    assert "add_wire" in rows["sch"]["tools"]
    assert CONTROL <= set(rows["core"]["tools"])


def test_menu_names_every_set():
    mcp, _ = _server()
    desc = mcp._tool_manager.get_tool("load_toolset").description
    for name in toolsets.TOOLSETS:
        if name != "core":
            assert f"{name}:" in desc


def test_server_advertises_tools_list_changed():
    mcp, _ = _server()
    caps = mcp._mcp_server.create_initialization_options().capabilities
    assert caps.tools.listChanged is True


def test_env_picks_startup_sets(monkeypatch):
    monkeypatch.setenv(toolsets.STARTUP_ENV, "core, library")
    mcp = FastMCP("t")
    manager = toolsets.install(mcp)
    assert list(manager.loaded) == ["core", "library"]


def _listed(mcp) -> dict:
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _size(tool) -> int:
    return len(json.dumps({"name": tool.name, "description": tool.description,
                           "inputSchema": tool.inputSchema}))


@pytest.fixture(scope="module")
def everything():
    mcp, _ = _server("all")
    return _listed(mcp)


def test_every_description_fits(everything):
    over = {
        name: len(t.description) for name, t in everything.items()
        if len(t.description) > (MENU_DESCRIPTION_MAX if name == "load_toolset"
                                 else DESCRIPTION_MAX)
    }
    assert not over, (
        f"descriptions over {DESCRIPTION_MAX} chars: {over}. Shorten the summary "
        f"and move the rest below a 'Details:' line in the docstring."
    )


def test_every_description_is_set(everything):
    assert not [n for n, t in everything.items() if len((t.description or "").strip()) < 20]


def test_every_tool_fits(everything):
    over = {n: _size(t) for n, t in everything.items() if _size(t) > TOOL_MAX}
    assert not over, f"tools over {TOOL_MAX} listed chars: {over}"


def test_every_set_fits_its_budget(everything):
    assert set(SET_BUDGET) == set(toolsets.TOOLSETS), "give a new set a budget"
    for name, tools in toolsets.catalog().items():
        members = set(tools) | (CONTROL | {"ping"} if name == "core" else set())
        used = sum(_size(everything[t]) for t in members if t in everything)
        assert used <= SET_BUDGET[name], f"set {name}: {used} chars > {SET_BUDGET[name]}"


def test_listed_schemas_are_compact(everything):
    for name, t in everything.items():
        text = json.dumps(t.inputSchema)
        assert '"title"' not in text, name
        assert '{"type": "null"}' not in text, name


def test_omitted_optional_argument_is_still_none():
    """The listed schema drops `anyOf [.., null]`; validation keeps None."""
    mcp, _ = _server("core,sch")
    tool = mcp._tool_manager.get_tool("add_symbol")
    assert "fields" not in tool.parameters["required"]
    model = tool.fn_metadata.arg_model
    args = model.model_validate({"lib_id": "Device:R", "reference": "R1", "value": "1k",
                                 "x_mm": 0, "y_mm": 0})
    assert args.fields is None


def test_summary_stops_at_details_marker():
    doc = "Summary line.\n\nMore summary.\n\nDetails:\n\nArgs:\n    x: y"
    assert toolsets.summary_of(doc) == "Summary line.\n\nMore summary."
    assert toolsets.summary_of("Only a summary.") == "Only a summary."
    assert toolsets.summary_of("Head.\nDetails:") == "Head."


def test_help_returns_full_doc_for_unloaded_tool():
    mcp, _ = _server()
    out = _call(mcp, "help", {"tool": "analyze_emc"})
    text = out[0].text if isinstance(out, list) else str(out)
    assert "toolset emc_si, not loaded" in text
    assert "EM-SPLIT-CROSS" in text  # detail that is not in the listed summary
    assert "EM-SPLIT-CROSS" not in toolsets.summary_of(toolsets.tool_doc("analyze_emc"))


def test_help_on_loaded_and_unknown_tool():
    mcp, _ = _server()
    out = _call(mcp, "help", {"tool": "run_drc"})
    text = out[0].text if isinstance(out, list) else str(out)
    assert text.startswith("run_drc (toolset core)")
    with pytest.raises(ToolError, match="did you mean"):
        _call(mcp, "help", {"tool": "run_drcc"})


def test_argument_literals_match_the_adapters():
    """A choice added to an adapter must show up in the tool schema too."""
    from typing import get_args

    from kicad_claude import arg_types as at
    from kicad_claude.adapters import circuit_review, connectivity_review, emc_review, spice_auto
    from kicad_claude.tools.sourcing import distributors as sourcing

    assert get_args(at.EmcGroup) == emc_review.GROUPS
    assert get_args(at.CircuitGroup) == circuit_review.GROUPS
    assert get_args(at.ConnectivityCheck) == connectivity_review.CHECKS
    assert get_args(at.SubcircuitType) == spice_auto.TYPES
    from kicad_claude.adapters import derating
    assert get_args(at.DeratingProfile) == derating.PROFILE_NAMES
    assert get_args(at.Source) == sourcing.SOURCE_NAMES


def test_list_arguments_are_lists(everything):
    """No comma-joined string lists are left in the v2.0 schemas."""
    for name in ("analyze_emc", "review_circuits", "review_connectivity",
                 "simulate_subcircuits", "check_availability", "sync_datasheets"):
        props = everything[name].inputSchema["properties"]
        listy = [p for p, s in props.items() if s.get("type") == "array"]
        assert listy, name
    assert everything["analyze_emc"].inputSchema["properties"]["groups"]["items"]["enum"] == [
        "ground", "switching", "decoupling", "io", "thermal"]


def test_slow_sync_tool_does_not_block_the_event_loop():
    """A minutes-long kicad-cli call used to freeze ping, tools/list and cancel."""
    import time

    mcp, _ = _server()

    @mcp.tool()
    def slow() -> str:
        """Sleep a while, like a long kicad-cli run."""
        time.sleep(0.4)
        return "done"

    async def scenario():
        start = time.perf_counter()
        task = asyncio.create_task(mcp._tool_manager.call_tool("slow", {}))
        await asyncio.sleep(0.05)
        tools = await mcp.list_tools()          # served while `slow` runs
        listed_after = time.perf_counter() - start
        assert await task == "done"
        return listed_after, len(tools)

    listed_after, n = asyncio.run(scenario())
    assert listed_after < 0.3 and n > 0


def test_sync_tools_still_run_one_at_a_time():
    """They share the active-project state and the same files."""
    import threading
    import time

    mcp, _ = _server()
    running, peak = [0], [0]
    guard = threading.Lock()

    @mcp.tool()
    def busy() -> str:
        """Count how many run at once."""
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.1)
        with guard:
            running[0] -= 1
        return "ok"

    async def scenario():
        await asyncio.gather(*(mcp._tool_manager.call_tool("busy", {}) for _ in range(3)))

    asyncio.run(scenario())
    assert peak[0] == 1
