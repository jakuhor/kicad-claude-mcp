"""Phase 3 — schematic editing.

Strategy:
- Unit tests for geometry, sch_io, sch_editor that don't need KiCAD.
- Integration tests that build a real schematic, write it, then verify with
  kicad-cli (marked @pytest.mark.slow).
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import sexpdata

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project, write_blank_schematic
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import schematic as sch_tools
from kicad_claude.utils.geometry import (
    file_to_pcb_xy,
    normalize_rotation,
    pcb_to_file_xy,
    rotate_xy,
    snap_xy,
)
from kicad_claude.utils.kicad_paths import find_kicad_cli, find_symbol_lib_dirs

FIXTURES = Path(__file__).parent / "fixtures"


# ===== Geometry ============================================================ #


def test_pcb_coords_are_kicad_native():
    """Issue 2: the PCB Y flip is gone — MCP coords are the file's own."""
    assert pcb_to_file_xy(100.0, 30.0) == (100.0, 30.0)
    assert file_to_pcb_xy(*pcb_to_file_xy(50, 70)) == (50.0, 70.0)


def test_pcb_grid_point_survives_the_transform():
    """The old flip around 297 mm (A3) knocked every grid point off-grid."""
    for y in (1.27, 25.4, 147.32):
        gx, gy = pcb_to_file_xy(0.0, y)
        assert snap_xy(gx, gy) == (0.0, y)


def test_normalize_rotation_accepts_right_angles():
    for r in (0, 90, 180, 270, 360, -90):
        normalize_rotation(r)


def test_normalize_rotation_rejects_other_angles():
    with pytest.raises(ValueError):
        normalize_rotation(45)


def test_rotate_xy_90_ccw():
    x, y = rotate_xy(1, 0, 90)
    assert math.isclose(x, 0, abs_tol=1e-9)
    assert math.isclose(y, 1, abs_tol=1e-9)


# ===== sch_io pretty-printer =============================================== #


def test_pretty_print_inline_atoms():
    import sexpdata

    node = [sexpdata.Symbol("at"), 39.37, 29.21, 0]
    assert sch_io.dumps(node) == "(at 39.37 29.21 0)"


def test_pretty_print_multiline_when_list_children():
    import sexpdata

    node = [
        sexpdata.Symbol("symbol"),
        "Foo",
        [sexpdata.Symbol("at"), 0, 0, 0],
        [sexpdata.Symbol("uuid"), "abc"],
    ]
    out = sch_io.dumps(node)
    assert "(symbol \"Foo\"\n" in out
    assert "\t(at 0 0 0)" in out
    assert "\t(uuid \"abc\")" in out


def test_pretty_print_round_trip_blank_project(tmp_path: Path):
    files = write_blank_project(tmp_path, "p")
    tree = sch_io.parse_file(files["sch"])
    out = tmp_path / "rt.kicad_sch"
    sch_io.write_file(out, tree)
    # Re-parse our own output — should give an equivalent tree.
    tree2 = sch_io.parse_file(out)
    assert sch_io.dumps(tree) == sch_io.dumps(tree2)


# ===== sch_editor on the blank template (no library lookups) =============== #


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "demo", "demo")
    state.set_active(tmp_path / "demo", "demo")
    yield files
    state.clear_active()


def test_add_wire_then_round_trip(blank_project):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)
    ed.add_wire(tree, 50, 50, 80, 50)
    sch_io.write_file(sch_path, tree)
    tree2 = sch_io.parse_file(sch_path)
    wires = sch_io.find_children(tree2, "wire")
    assert len(wires) == 1


def test_add_label_then_read_back(blank_project):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)
    ed.add_label(tree, "VBUS", 60, 100, "right")
    sch_io.write_file(sch_path, tree)
    tree2 = sch_io.parse_file(sch_path)
    labels = sch_io.find_children(tree2, "label")
    assert len(labels) == 1
    assert labels[0][1] == "VBUS"


def test_backup_creates_dot_backups_dir(blank_project):
    sch_path = blank_project["sch"]
    backup = ed.backup_file(sch_path)
    assert backup is not None
    assert backup.parent.name == ".backups"
    assert backup.is_file()


# ===== add_symbol with a fixture lib (no real KiCAD libs needed) =========== #


def _patched_index_with_minilib(tmp_path):
    """Build an index pointing only at our MiniLib.kicad_sym fixture."""
    sym_dir = tmp_path / "syms"
    sym_dir.mkdir()
    shutil.copy(FIXTURES / "MiniLib.kicad_sym", sym_dir / "MiniLib.kicad_sym")
    return kicad_libs.build_index(symbol_dirs=[sym_dir], footprint_dirs=[])


def test_add_symbol_via_editor(blank_project, tmp_path, monkeypatch):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)

    # Fetch lib symbol def from the fixture.
    sym_def = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    ed.add_symbol(
        tree,
        qualified_lib_id="MiniLib:Resistor",
        reference="R1",
        value="10k",
        x_mm=100,
        y_mm=80,
        rotation=0,
        sym_def_node=sym_def,
        project_name="demo",
    )
    sch_io.write_file(sch_path, tree)

    tree2 = sch_io.parse_file(sch_path)
    s = ed.find_symbol_by_reference(tree2, "R1")
    assert s is not None
    assert ed.get_symbol_property(s, "Value") == "10k"
    # lib_symbols injected
    assert ed.find_lib_symbol_def(tree2, "MiniLib:Resistor") is not None


def test_get_pin_position_for_symbol_at_origin(blank_project):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)
    sym_def = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    # Our MiniLib resistor has pin1 at local (0, 2.54) and pin2 at (0, -2.54).
    # Symbol Y is "down" in lib coords.
    ed.add_symbol(
        tree,
        qualified_lib_id="MiniLib:Resistor",
        reference="R1",
        value="10k",
        x_mm=100,
        y_mm=100,
        rotation=0,
        sym_def_node=sym_def,
        project_name="demo",
    )
    pins = ed.list_pins_for_symbol(tree, "R1")
    assert len(pins) == 2
    by_num = {p["number"]: p for p in pins}
    # In MCP coords (Y up), the lib pin at lib-y=2.54 is BELOW symbol origin
    # (KiCAD lib Y is "down"), so MCP-y < 100. Pin at lib-y=-2.54 is above.
    assert by_num["1"]["position_mm"][0] == 100.0
    assert by_num["2"]["position_mm"][0] == 100.0
    # Symmetry: pin1 and pin2 mirror around symbol y
    y1 = by_num["1"]["position_mm"][1]
    y2 = by_num["2"]["position_mm"][1]
    assert math.isclose(y1 + y2, 200.0, abs_tol=0.01)  # 2 * symbol_y_mcp


def test_remove_symbol_returns_false_when_missing(blank_project):
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.remove_symbol(tree, "DOES_NOT_EXIST") is False


def test_duplicate_reference_rejected(blank_project):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)
    sym_def = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    ed.add_symbol(
        tree,
        qualified_lib_id="MiniLib:Resistor",
        reference="R1",
        value="10k",
        x_mm=100,
        y_mm=80,
        rotation=0,
        sym_def_node=sym_def,
        project_name="demo",
    )
    # Second R1 should fail — even with a fresh def.
    sym_def2 = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    with pytest.raises(ValueError, match="already exists"):
        ed.add_symbol(
            tree,
            qualified_lib_id="MiniLib:Resistor",
            reference="R1",
            value="other",
            x_mm=120,
            y_mm=80,
            rotation=0,
            sym_def_node=sym_def2,
            project_name="demo",
        )


# ===== Tool layer (via FastMCP) ============================================ #


def _make_mcp_with_fixture_index(monkeypatch, tmp_path):
    """Patch lib_tools to expose the MiniLib fixture as the only indexed lib."""
    from mcp.server.fastmcp import FastMCP

    idx = _patched_index_with_minilib(tmp_path)
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)

    mcp = FastMCP("test")
    sch_tools.register(mcp)
    lib_tools.register(mcp)
    return mcp


def _call(mcp, name, **kwargs):
    return mcp._tool_manager.get_tool(name).fn(**kwargs)


def test_add_symbol_tool_writes_schematic(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    res = _call(
        mcp,
        "add_symbol",
        lib_id="MiniLib:Resistor",
        reference="R1",
        value="10k",
        x_mm=100,
        y_mm=80,
    )
    assert res["reference"] == "R1"
    assert res["pin_count"] == 2
    # Read the schematic back to confirm persisted
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.find_symbol_by_reference(tree, "R1") is not None


def test_get_pin_position_tool(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="10k", x_mm=100, y_mm=100)
    res = _call(mcp, "get_pin_position", reference="R1", pin="1")
    assert res["reference"] == "R1"
    assert res["pin"] == "1"
    # 100 mm snaps to 100.33 (79 x 1.27); pins sit on the same vertical line.
    assert res["position_mm"][0] == 100.33


def test_move_then_remove_via_tools(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1", value="10k", x_mm=100, y_mm=100)
    _call(mcp, "move_symbol", reference="R1", x_mm=50, y_mm=60, rotation=90)
    tree = sch_io.parse_file(blank_project["sch"])
    s = ed.find_symbol_by_reference(tree, "R1")
    at = sch_io.find_child(s, "at")
    # Native KiCAD coords, snapped to 1.27 mm: 50 -> 49.53, 60 -> 59.69.
    assert at[1] == 49.53
    assert at[2] == 59.69
    assert at[3] == 90
    # Now remove
    _call(mcp, "remove_symbol", reference="R1")
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.find_symbol_by_reference(tree, "R1") is None


# ===== Acceptance: voltage divider (slow, real KiCAD libs) ================= #


@pytest.mark.slow
def test_voltage_divider_acceptance(tmp_path):
    """End-to-end: build a 10k/1k divider between +5V and GND. kicad-cli must parse."""
    cli = find_kicad_cli()
    if not cli or not find_symbol_lib_dirs():
        pytest.skip("no kicad-cli or KiCAD symbol libs available")

    # Need a real library index (from cache if it exists, else build it).
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built; run `index_libraries` first")
    state.clear_active()
    lib_tools._index = cached  # warm tool memo

    files = write_blank_project(tmp_path / "divider", "divider")
    state.set_active(tmp_path / "divider", "divider")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")
    sch_tools.register(mcp)
    lib_tools.register(mcp)

    # Layout: vertical strip at x=100, +5V at top (y=160), GND at bottom (y=40).
    _call(mcp, "add_power_symbol", net="+5V", x_mm=100, y_mm=160)
    _call(mcp, "add_symbol", lib_id="Device:R", reference="R1", value="10k",
          x_mm=100, y_mm=130)
    _call(mcp, "add_symbol", lib_id="Device:R", reference="R2", value="1k",
          x_mm=100, y_mm=80)
    _call(mcp, "add_power_symbol", net="GND", x_mm=100, y_mm=40)

    # Wire R1 between +5V and the mid-node
    _call(mcp, "add_wire", x1_mm=100, y1_mm=160, x2_mm=100, y2_mm=140)  # +5V → R1.top
    _call(mcp, "add_wire", x1_mm=100, y1_mm=120, x2_mm=100, y2_mm=90)   # R1.bot → R2.top
    _call(mcp, "add_wire", x1_mm=100, y1_mm=70, x2_mm=100, y2_mm=40)    # R2.bot → GND

    sch_path = files["sch"]
    state.clear_active()

    # kicad-cli must parse the file (returncode 0). ERC violations are OK.
    r = subprocess.run(
        [str(cli), "sch", "erc", str(sch_path)],
        capture_output=True, text=True, timeout=60,
        cwd=tmp_path,
    )
    assert r.returncode == 0, f"erc failed: stderr={r.stderr}"


# ===== Issue 1 — control characters in string literals ===================== #


def test_escape_control_characters():
    r"""`sexpdata` decodes \n on read; a naive dump would write a raw newline.

    A TAB stays raw — that is what KiCAD itself writes.
    """
    node = [sch_io.sym("text"), "TODO:\n+ USB PD\t- \"EMI\"\\filter\r"]
    out = sch_io.dumps(node)
    assert "\n" not in out[out.index('"'):]  # no raw newline inside the literal
    assert out == '(text "TODO:\\n+ USB PD\t- \\"EMI\\"\\\\filter\\r")'


def test_multiline_text_round_trip(tmp_path: Path):
    files = write_blank_project(tmp_path, "p")
    tree = sch_io.parse_file(files["sch"])
    original = 'line1\nline2\twith "quotes" and \\ backslash'
    tree.append([sch_io.sym("text"), original, [sch_io.sym("uuid"), "u1"]])
    sch_io.write_file(files["sch"], tree)

    tree2 = sch_io.parse_file(files["sch"])
    texts = sch_io.find_children(tree2, "text")
    assert len(texts) == 1
    assert texts[0][1] == original


@pytest.mark.slow
def test_multiline_text_still_loads_in_kicad_cli(tmp_path: Path):
    """Regression for the blocker: a re-written sheet with \n must still load."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("no kicad-cli available")
    files = write_blank_project(tmp_path / "mt", "mt")
    tree = sch_io.parse_file(files["sch"])
    tree.append([
        sch_io.sym("text"),
        "TODO:\n+ USB PD\n- EMI filter design",
        [sch_io.sym("at"), 100.0, 100.0, 0],
        [sch_io.sym("effects"), [sch_io.sym("font"), [sch_io.sym("size"), 1.27, 1.27]]],
        [sch_io.sym("uuid"), "00000000-0000-0000-0000-000000000001"],
    ])
    sch_io.write_file(files["sch"], tree)

    r = subprocess.run(
        [str(cli), "sch", "erc", str(files["sch"])],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert r.returncode == 0, f"erc failed: stderr={r.stderr}"


# ===== Issue 5 — #PWR references unique across the hierarchy =============== #


def test_hierarchy_sch_paths_walks_subsheets(tmp_path: Path):
    files = write_blank_project(tmp_path / "h", "h")
    root = files["sch"]
    child = root.parent / "child.kicad_sch"
    write_blank_schematic(child)

    tree = sch_io.parse_file(root)
    ed.add_sheet_node(tree, sheet_name="child", sheet_filename="child.kicad_sch",
                      x_mm=50, y_mm=150, width_mm=30, height_mm=20,
                      project_name="h")
    sch_io.write_file(root, tree)

    paths = ed.hierarchy_sch_paths(root)
    assert [p.name for p in paths] == ["h.kicad_sch", "child.kicad_sch"]


def test_next_power_reference_skips_numbers_used_on_other_sheets(tmp_path, monkeypatch):
    idx = _patched_index_with_minilib(tmp_path)
    monkeypatch.setattr(lib_tools, "_index", idx)

    state.clear_active()
    files = write_blank_project(tmp_path / "h2", "h2")
    root = files["sch"]
    child = root.parent / "child.kicad_sch"
    write_blank_schematic(child)
    state.set_active(tmp_path / "h2", "h2")
    try:
        # Child sheet already owns #PWR0001.
        child_tree = sch_io.parse_file(child)
        sym_def = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
        ed.add_symbol(
            child_tree, qualified_lib_id="MiniLib:Resistor", reference="#PWR0001",
            value="GND", x_mm=50, y_mm=50, rotation=0, sym_def_node=sym_def,
            project_name="h2", instance_path="/",
        )
        sch_io.write_file(child, child_tree)

        root_tree = sch_io.parse_file(root)
        ed.add_sheet_node(root_tree, sheet_name="child", sheet_filename="child.kicad_sch",
                          x_mm=50, y_mm=150, width_mm=30, height_mm=20,
                          project_name="h2")
        sch_io.write_file(root, root_tree)

        assert sch_tools._next_power_reference(sch_io.parse_file(root)) == "#PWR0002"
    finally:
        state.clear_active()


# ===== Issue 2 — native coordinates and grid snapping ===================== #


def test_sch_to_file_xy_is_identity():
    from kicad_claude.utils.geometry import file_to_sch_xy, sch_to_file_xy

    assert sch_to_file_xy(228.6, 147.32) == (228.6, 147.32)
    assert file_to_sch_xy(*sch_to_file_xy(10, 20)) == (10.0, 20.0)


def test_snap_mm_rounds_to_grid():
    from kicad_claude.utils.geometry import snap_mm, snap_xy

    assert snap_mm(147.32) == 147.32           # already on grid (116 x 1.27)
    assert snap_mm(147.5) == 147.32
    assert snap_mm(0) == 0
    assert snap_xy(100.0, 149.68) == (100.33, 149.86)


def test_snap_mm_rejects_non_positive_grid():
    from kicad_claude.utils.geometry import snap_mm

    with pytest.raises(ValueError):
        snap_mm(10.0, grid_mm=0)


def test_placement_lands_on_grid_and_keeps_y_down(blank_project, tmp_path, monkeypatch):
    """Issue 2: a grid-aligned input must stay grid-aligned in the file."""
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1",
          value="10k", x_mm=228.6, y_mm=147.32)
    tree = sch_io.parse_file(blank_project["sch"])
    at = sch_io.find_child(ed.find_symbol_by_reference(tree, "R1"), "at")
    # Y is written as given — no page-height flip — and both are grid multiples.
    assert (at[1], at[2]) == (228.6, 147.32)
    for value in (at[1], at[2]):
        assert math.isclose(value / 1.27, round(value / 1.27), abs_tol=1e-6)


def test_snap_to_grid_false_keeps_exact_position(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1",
          value="10k", x_mm=100.0, y_mm=100.0, snap_to_grid=False)
    tree = sch_io.parse_file(blank_project["sch"])
    at = sch_io.find_child(ed.find_symbol_by_reference(tree, "R1"), "at")
    assert (at[1], at[2]) == (100.0, 100.0)


def test_add_wire_snaps_both_endpoints(blank_project):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("t")
    sch_tools.register(mcp)
    res = mcp._tool_manager.get_tool("add_wire").fn(
        x1_mm=100.0, y1_mm=50.0, x2_mm=130.0, y2_mm=50.0
    )
    assert res["from_mm"] == [100.33, 49.53]
    assert res["to_mm"] == [129.54, 49.53]
    tree = sch_io.parse_file(blank_project["sch"])
    pts = sch_io.find_child(sch_io.find_children(tree, "wire")[0], "pts")
    assert [pts[1][1], pts[1][2]] == [100.33, 49.53]


# ===== Issue 6 — deletion primitives ====================================== #


def _mcp_with_sch_tools():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("t")
    sch_tools.register(mcp)
    return mcp


def test_remove_wire_matches_either_endpoint_order(blank_project):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)
    ed.add_wire(tree, 50, 50, 80, 50)
    sch_io.write_file(sch_path, tree)

    mcp = _mcp_with_sch_tools()
    res = mcp._tool_manager.get_tool("remove_wire").fn(
        x1_mm=80, y1_mm=50, x2_mm=50, y2_mm=50, snap_to_grid=False
    )
    assert res["removed"] == "wire"
    tree2 = sch_io.parse_file(sch_path)
    assert sch_io.find_children(tree2, "wire") == []


def test_remove_wire_missing_raises(blank_project):
    mcp = _mcp_with_sch_tools()
    with pytest.raises(KeyError):
        mcp._tool_manager.get_tool("remove_wire").fn(
            x1_mm=1, y1_mm=1, x2_mm=2, y2_mm=2, snap_to_grid=False
        )


def test_add_junction_is_idempotent(blank_project):
    mcp = _mcp_with_sch_tools()
    first = mcp._tool_manager.get_tool("add_junction").fn(x_mm=100.33, y_mm=50.8)
    second = mcp._tool_manager.get_tool("add_junction").fn(x_mm=100.33, y_mm=50.8)
    assert first["created"] is True
    assert second["created"] is False
    tree = sch_io.parse_file(blank_project["sch"])
    assert len(sch_io.find_children(tree, "junction")) == 1

    removed = mcp._tool_manager.get_tool("remove_junction").fn(x_mm=100.33, y_mm=50.8)
    assert removed["removed"] == "junction"
    tree = sch_io.parse_file(blank_project["sch"])
    assert sch_io.find_children(tree, "junction") == []


def test_remove_items_in_box_keeps_crossing_wire(blank_project):
    sch_path = blank_project["sch"]
    tree = sch_io.parse_file(sch_path)
    ed.add_wire(tree, 50, 50, 60, 50)       # fully inside
    ed.add_wire(tree, 50, 60, 200, 60)      # crosses the right edge
    ed.add_junction(tree, 55, 50)           # inside
    ed.add_label(tree, "VBUS", 300, 300)    # far outside
    sch_io.write_file(sch_path, tree)

    mcp = _mcp_with_sch_tools()
    res = mcp._tool_manager.get_tool("remove_items_in_box").fn(
        x1_mm=40, y1_mm=40, x2_mm=100, y2_mm=100
    )
    assert res["removed"] == {"wire": 1, "junction": 1}
    tree2 = sch_io.parse_file(sch_path)
    assert len(sch_io.find_children(tree2, "wire")) == 1
    assert len(sch_io.find_children(tree2, "label")) == 1


def test_remove_items_in_box_rejects_unknown_kind(blank_project):
    mcp = _mcp_with_sch_tools()
    with pytest.raises(ValueError, match="unknown kinds"):
        mcp._tool_manager.get_tool("remove_items_in_box").fn(
            x1_mm=0, y1_mm=0, x2_mm=10, y2_mm=10, kinds=["footprint"]
        )


def test_remove_items_in_box_skips_symbols_by_default(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1",
          value="10k", x_mm=50.8, y_mm=50.8)
    _call(mcp, "remove_items_in_box", x1_mm=0, y1_mm=0, x2_mm=100, y2_mm=100)
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.find_symbol_by_reference(tree, "R1") is not None

    _call(mcp, "remove_items_in_box", x1_mm=0, y1_mm=0, x2_mm=100, y2_mm=100,
          kinds=["symbol"])
    tree = sch_io.parse_file(blank_project["sch"])
    assert ed.find_symbol_by_reference(tree, "R1") is None


def test_remove_symbol_cleans_dangling_stubs_only(blank_project, tmp_path, monkeypatch):
    """Issue 6: stubs on the removed pins go; a wire still reaching R2 stays."""
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1",
          value="10k", x_mm=100.33, y_mm=100.33)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R2",
          value="1k", x_mm=100.33, y_mm=120.65)

    tree = sch_io.parse_file(blank_project["sch"])
    r1_pins = {p["number"]: p["position_mm"] for p in ed.list_pins_for_symbol(tree, "R1")}
    r2_pins = {p["number"]: p["position_mm"] for p in ed.list_pins_for_symbol(tree, "R2")}
    top = r1_pins["1"] if r1_pins["1"][1] < r1_pins["2"][1] else r1_pins["2"]
    bottom = r1_pins["2"] if r1_pins["1"][1] < r1_pins["2"][1] else r1_pins["1"]
    r2_top = r2_pins["1"] if r2_pins["1"][1] < r2_pins["2"][1] else r2_pins["2"]

    # Stub above R1 (connects to nothing) and a wire from R1's bottom pin to R2.
    _call(mcp, "add_wire", x1_mm=top[0], y1_mm=top[1],
          x2_mm=top[0], y2_mm=top[1] - 5.08, snap_to_grid=False)
    _call(mcp, "add_wire", x1_mm=bottom[0], y1_mm=bottom[1],
          x2_mm=r2_top[0], y2_mm=r2_top[1], snap_to_grid=False)
    _call(mcp, "add_no_connect", reference="R1", pin="1")

    res = _call(mcp, "remove_symbol", reference="R1", remove_connected_wires=True)
    assert res["removed_wires"] == 1       # the dangling stub only
    assert res["removed_no_connects"] == 1

    tree = sch_io.parse_file(blank_project["sch"])
    wires = sch_io.find_children(tree, "wire")
    assert len(wires) == 1                 # the R1-R2 wire survives (still on R2)
    assert sch_io.find_children(tree, "no_connect") == []
    assert ed.find_symbol_by_reference(tree, "R1") is None


def test_remove_symbol_leaves_wires_when_not_asked(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1",
          value="10k", x_mm=100.33, y_mm=100.33)
    tree = sch_io.parse_file(blank_project["sch"])
    pin = ed.list_pins_for_symbol(tree, "R1")[0]["position_mm"]
    _call(mcp, "add_wire", x1_mm=pin[0], y1_mm=pin[1],
          x2_mm=pin[0] + 10.16, y2_mm=pin[1], snap_to_grid=False)

    res = _call(mcp, "remove_symbol", reference="R1")
    assert res["removed_wires"] == 0
    tree = sch_io.parse_file(blank_project["sch"])
    assert len(sch_io.find_children(tree, "wire")) == 1


@pytest.mark.slow
def test_deletion_round_trip_still_loads(tmp_path, monkeypatch):
    """A sheet edited by the deletion tools must still load in kicad-cli."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("no kicad-cli available")
    state.clear_active()
    files = write_blank_project(tmp_path / "del", "del")
    state.set_active(tmp_path / "del", "del")
    try:
        mcp = _make_mcp_with_fixture_index(monkeypatch, tmp_path)
        _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1",
              value="10k", x_mm=100.33, y_mm=100.33)
        _call(mcp, "add_wire", x1_mm=50.8, y1_mm=50.8, x2_mm=76.2, y2_mm=50.8)
        _call(mcp, "add_junction", x_mm=63.5, y_mm=50.8)
        _call(mcp, "remove_junction", x_mm=63.5, y_mm=50.8)
        _call(mcp, "remove_wire", x1_mm=50.8, y1_mm=50.8, x2_mm=76.2, y2_mm=50.8)
        _call(mcp, "remove_symbol", reference="R1", remove_connected_wires=True)
        sch_path = files["sch"]
    finally:
        state.clear_active()

    r = subprocess.run(
        [str(cli), "sch", "erc", str(sch_path)],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert r.returncode == 0, f"erc failed: stderr={r.stderr}"


# ===== Issue 8 — output matches KiCAD 10's own formatting ================== #

KICAD10_FIXTURE = FIXTURES / "kicad10_ecc83-pp_v2.kicad_sch"


def test_dump_of_kicad_written_file_is_byte_identical():
    """parse -> dump of a file KiCAD 10 wrote must reproduce it exactly."""
    original = KICAD10_FIXTURE.read_text(encoding="utf-8")
    tree = sch_io.parse_file(KICAD10_FIXTURE)
    assert sch_io.dumps(tree) + "\n" == original


def test_write_file_of_kicad_written_file_changes_nothing(tmp_path: Path):
    """The same, through write_file — a no-op edit must leave the file alone."""
    target = tmp_path / "copy.kicad_sch"
    target.write_bytes(KICAD10_FIXTURE.read_bytes())
    before = target.read_text(encoding="utf-8")
    sch_io.write_file(target, sch_io.parse_file(target))
    assert target.read_text(encoding="utf-8") == before


def test_pts_points_are_packed_and_wrapped():
    pts = [sch_io.sym("pts")] + [
        [sch_io.sym("xy"), float(i), 0.0] for i in range(12)
    ]
    out = sch_io.dumps([sch_io.sym("polyline"), pts], 0)
    point_lines = [ln for ln in out.splitlines() if ln.lstrip("\t").startswith("(xy ")]
    assert len(point_lines) < 12          # packed, not one per line
    assert all(len(ln) <= sch_io.LINE_WIDTH for ln in point_lines)


def test_data_chunks_are_one_per_line():
    node = [sch_io.sym("data"), "A" * 76, "B" * 76, "C" * 4]
    lines = sch_io.dumps(node).splitlines()
    assert lines[0] == '(data "' + "A" * 76 + '"'
    assert lines[1] == "\t" + '"' + "B" * 76 + '"'
    assert lines[2] == "\t" + '"' + "C" * 4 + '"'   # short chunk still alone
    assert lines[3] == ")"


def test_sheet_fill_alpha_has_four_decimals():
    sheet = [
        sch_io.sym("sheet"),
        [sch_io.sym("at"), 0, 0],
        [sch_io.sym("fill"), [sch_io.sym("color"), 0, 0, 0, 0.0]],
    ]
    assert "(color 0 0 0 0.0000)" in sch_io.dumps(sheet)
    # Outside a sheet's fill, KiCAD writes the alpha plainly.
    junction = [sch_io.sym("junction"), [sch_io.sym("color"), 0, 0, 0, 0.0]]
    assert "(color 0 0 0 0)" in sch_io.dumps(junction)


def test_float_keeps_full_precision():
    node = [sch_io.sym("at"), 59.209102362204725, 270]
    assert sch_io.dumps(node) == "(at 59.209102362204725 270)"


# --------------------------------------------------------------------------- #
# KiCAD `{brace}` escapes and `(property private ...)`
# --------------------------------------------------------------------------- #


class TestBraceEscapes:
    """`utils/kicad_strings.py` — KiCAD's own name codec."""

    def test_unescape_known_sequences(self):
        from kicad_claude.utils.kicad_strings import unescape_braces

        assert unescape_braces("VBUS{slash}5V") == "VBUS/5V"
        assert unescape_braces("A{dblquote}B") == 'A"B'
        assert unescape_braces("{lt}x{gt}") == "<x>"
        assert unescape_braces("a{space}b") == "a b"

    def test_unescape_leaves_unknown_sequence_alone(self):
        from kicad_claude.utils.kicad_strings import unescape_braces

        assert unescape_braces("NET{foo}1") == "NET{foo}1"

    def test_unescape_is_a_noop_without_braces(self):
        from kicad_claude.utils.kicad_strings import unescape_braces

        assert unescape_braces("VBUS_5V") == "VBUS_5V"

    def test_escape_round_trips(self):
        from kicad_claude.utils.kicad_strings import escape_braces, unescape_braces

        for raw in ["VBUS/5V", 'A"B', "<x>", "a b", "p{q}", "a|b:c", "tab\there"]:
            assert unescape_braces(escape_braces(raw)) == raw

    def test_escape_handles_literal_brace_first(self):
        from kicad_claude.utils.kicad_strings import escape_braces

        # A literal `{` must not be left to look like the start of a sequence.
        assert escape_braces("{slash}") == "{brace}slash}"

    def test_normalize_matches_both_spellings(self):
        from kicad_claude.utils.kicad_strings import normalize_name

        assert normalize_name("VBUS{slash}5V") == normalize_name("VBUS/5V")


class TestPropertyPrivateOffset:
    """KiCAD 9+ may write `(property private "Name" "Value" ...)`."""

    @staticmethod
    def _symbol(private: bool) -> list:
        head = [sexpdata.Symbol("property")]
        if private:
            head.append(sexpdata.Symbol("private"))
        return [
            sexpdata.Symbol("symbol"),
            [sexpdata.Symbol("lib_id"), "Device:R"],
            head + ["Reference", "R1", [sexpdata.Symbol("at"), 0, 0, 0]],
            [sexpdata.Symbol("property"), "Value", "10k"],
        ]

    def test_property_name_index(self):
        plain = [sexpdata.Symbol("property"), "Reference", "R1"]
        private = [sexpdata.Symbol("property"), sexpdata.Symbol("private"), "Reference", "R1"]
        assert sch_io.property_name_index(plain) == 1
        assert sch_io.property_name_index(private) == 2

    @pytest.mark.parametrize("private", [False, True])
    def test_get_property(self, private):
        node = self._symbol(private)
        assert sch_io.get_property(node, "Reference") == "R1"
        assert sch_io.get_property(node, "Value") == "10k"
        assert sch_io.get_property(node, "Nope") is None

    @pytest.mark.parametrize("private", [False, True])
    def test_get_properties_lowercases_keys(self, private):
        props = sch_io.get_properties(self._symbol(private))
        assert props["reference"] == "R1"
        assert props["value"] == "10k"

    @pytest.mark.parametrize("private", [False, True])
    def test_set_symbol_property_writes_the_value_slot(self, private):
        node = self._symbol(private)
        ed.set_symbol_property(node, "Reference", "R7")
        assert sch_io.get_property(node, "Reference") == "R7"
        # The `private` token itself must survive.
        prop = sch_io.find_children(node, "property")[0]
        assert sch_io.is_symbol(prop[1], "private") is private

    @pytest.mark.parametrize("private", [False, True])
    def test_find_symbol_by_reference(self, private):
        tree = [sexpdata.Symbol("kicad_sch"), self._symbol(private)]
        assert ed.find_symbol_by_reference(tree, "R1") is not None
        assert ed.find_symbol_by_reference(tree, "R9") is None

    def test_find_symbol_by_reference_matches_escaped_form(self):
        node = [
            sexpdata.Symbol("symbol"),
            [sexpdata.Symbol("lib_id"), "Device:R"],
            [sexpdata.Symbol("property"), "Reference", "R{slash}1"],
        ]
        tree = [sexpdata.Symbol("kicad_sch"), node]
        assert ed.find_symbol_by_reference(tree, "R/1") is node
        assert ed.find_symbol_by_reference(tree, "R{slash}1") is node


class TestHasFlag:
    """Three spellings of a boolean flag across KiCAD versions."""

    def test_bare_token(self):
        node = [sexpdata.Symbol("pin"), sexpdata.Symbol("hide")]
        assert sch_io.has_flag(node, "hide") is True

    def test_boolean_yes(self):
        node = [sexpdata.Symbol("pin"), [sexpdata.Symbol("hide"), sexpdata.Symbol("yes")]]
        assert sch_io.has_flag(node, "hide") is True

    def test_boolean_no(self):
        node = [sexpdata.Symbol("pin"), [sexpdata.Symbol("hide"), sexpdata.Symbol("no")]]
        assert sch_io.has_flag(node, "hide") is False

    def test_absent(self):
        node = [sexpdata.Symbol("pin"), [sexpdata.Symbol("at"), 0, 0]]
        assert sch_io.has_flag(node, "hide") is False


class TestFindDeep:
    def test_finds_nested_nodes_outermost_first(self):
        tree = sexpdata.loads("(a (b 1) (c (b 2) (d (b 3))))")
        found = sch_io.find_deep(tree, "b")
        assert [n[1] for n in found] == [1, 2, 3]

    def test_returns_empty_for_missing_head(self):
        assert sch_io.find_deep(sexpdata.loads("(a (b 1))"), "zz") == []

    def test_tolerates_an_atom(self):
        assert sch_io.find_deep(sexpdata.Symbol("a"), "b") == []


class TestParseFileErrors:
    """`parse_file` must name the file it could not read."""

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.kicad_sch"
        p.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            sch_io.parse_file(p)

    def test_truncated_file(self, tmp_path):
        p = tmp_path / "trunc.kicad_sch"
        p.write_text("(kicad_sch (version 20250114) (symbol", encoding="utf-8")
        with pytest.raises(ValueError, match="well-formed"):
            sch_io.parse_file(p)

    def test_good_file_still_parses(self, tmp_path):
        p = tmp_path / "ok.kicad_sch"
        p.write_text("(kicad_sch (version 20250114))", encoding="utf-8")
        assert sch_io.head_of(sch_io.parse_file(p)) == "kicad_sch"


class TestLineEndingsPreserved:
    """Issue 8: a write must not flip the whole file's line endings."""

    FIXTURE = FIXTURES / "kicad10_ecc83-pp_v2.kicad_sch"

    def _rewrite(self, tmp_path, raw: bytes) -> bytes:
        target = tmp_path / "sheet.kicad_sch"
        target.write_bytes(raw)
        sch_io.write_file(target, sch_io.parse_file(target))
        return target.read_bytes()

    def test_lf_file_stays_lf(self, tmp_path):
        raw = self.FIXTURE.read_bytes().replace(b"\r\n", b"\n")
        out = self._rewrite(tmp_path, raw)
        assert out == raw
        assert b"\r\n" not in out

    def test_crlf_file_stays_crlf(self, tmp_path):
        raw = self.FIXTURE.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        out = self._rewrite(tmp_path, raw)
        assert out == raw
        assert out.count(b"\r\n") == out.count(b"\n")

    def test_detect_newline_on_a_missing_file_is_the_platform_default(self, tmp_path):
        assert sch_io.detect_newline(tmp_path / "nope.kicad_sch") == os.linesep

    def test_mixed_endings_take_the_majority(self, tmp_path):
        p = tmp_path / "mixed.kicad_sch"
        p.write_bytes(b"(a\r\n(b 1)\r\n(c 2)\n)\r\n")
        assert sch_io.detect_newline(p) == "\r\n"
