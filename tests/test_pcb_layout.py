"""PCB editing.

Strategy:
- Unit tests against the blank-template PCB and a synthetic footprint def.
- Integration test: build a tiny board with real KiCAD library footprints
  and verify kicad-cli pcb drc parses it (marked @pytest.mark.slow).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import sexpdata

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools.library import index as lib_tools
from kicad_claude.tools.pcb import layout as pcb_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli, find_footprint_lib_dirs

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "demo", "demo")
    state.set_active(tmp_path / "demo", "demo")
    yield files
    state.clear_active()


# ===== Board outline ======================================================= #


def test_set_board_outline_appends_gr_rect_on_edge_cuts(blank_project):
    pcb = blank_project["pcb"]
    tree = sch_io.parse_file(pcb)
    result = ed.set_board_outline(tree, 50, 30)
    assert result["width_mm"] == 50
    assert result["height_mm"] == 30

    rects = sch_io.find_children(tree, "gr_rect")
    edge_rects = [r for r in rects if (sch_io.find_child(r, "layer") or [None, ""])[1] == "Edge.Cuts"]
    assert len(edge_rects) == 1


def test_set_board_outline_replaces_previous_outline(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    ed.set_board_outline(tree, 50, 30)
    ed.set_board_outline(tree, 80, 40)
    edge_rects = [
        r for r in sch_io.find_children(tree, "gr_rect")
        if (sch_io.find_child(r, "layer") or [None, ""])[1] == "Edge.Cuts"
    ]
    assert len(edge_rects) == 1


def test_set_board_outline_rejects_unknown_shape(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    with pytest.raises(ValueError):
        ed.set_board_outline(tree, 50, 30, shape="circle")


# ===== Footprint placement (synthetic .kicad_mod) ========================== #


def _make_synth_fp_def() -> list:
    """Parse the MiniFP fixture as a footprint def."""
    return ed.fetch_footprint_def(FIXTURES / "MiniFP.pretty" / "Mini_R_0603.kicad_mod")


def test_add_footprint_via_editor(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    fp_def = _make_synth_fp_def()
    ed.add_footprint(
        tree,
        qualified_lib_id="MiniFP:Mini_R_0603",
        reference="R1",
        value="10k",
        x_mm=20,
        y_mm=15,
        rotation=0,
        layer="F.Cu",
        fp_def_node=fp_def,
    )
    fp = ed.find_footprint_by_reference(tree, "R1")
    assert fp is not None
    layer = sch_io.find_child(fp, "layer")
    assert layer[1] == "F.Cu"
    at = sch_io.find_child(fp, "at")
    # Issue 2: coordinates are KiCAD-native, no Y flip.
    assert at[1] == 20.0
    assert at[2] == 15.0


def test_add_footprint_rejects_bad_layer(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    with pytest.raises(ValueError, match="layer"):
        ed.add_footprint(
            tree, qualified_lib_id="x:y", reference="R1", value="v",
            x_mm=0, y_mm=0, layer="F.Bogus", fp_def_node=_make_synth_fp_def()
        )


def test_duplicate_footprint_reference_rejected(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    fp_def = _make_synth_fp_def()
    ed.add_footprint(
        tree, qualified_lib_id="MiniFP:Mini_R_0603", reference="R1",
        value="10k", x_mm=10, y_mm=10, fp_def_node=fp_def,
    )
    with pytest.raises(ValueError, match="already exists"):
        ed.add_footprint(
            tree, qualified_lib_id="MiniFP:Mini_R_0603", reference="R1",
            value="other", x_mm=20, y_mm=20, fp_def_node=_make_synth_fp_def(),
        )


def test_remove_footprint(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    fp_def = _make_synth_fp_def()
    ed.add_footprint(
        tree, qualified_lib_id="MiniFP:Mini_R_0603", reference="R1",
        value="10k", x_mm=10, y_mm=10, fp_def_node=fp_def,
    )
    assert ed.remove_footprint(tree, "R1") is True
    assert ed.remove_footprint(tree, "DNE") is False


def test_move_footprint(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    fp_def = _make_synth_fp_def()
    ed.add_footprint(
        tree, qualified_lib_id="MiniFP:Mini_R_0603", reference="R1",
        value="10k", x_mm=10, y_mm=10, fp_def_node=fp_def,
    )
    ed.move_footprint(tree, "R1", x_mm=30, y_mm=25, rotation=90, layer="B.Cu")
    fp = ed.find_footprint_by_reference(tree, "R1")
    at = sch_io.find_child(fp, "at")
    layer = sch_io.find_child(fp, "layer")
    assert at[1] == 30.0
    assert at[2] == 25.0
    assert at[3] == 90
    assert layer[1] == "B.Cu"


# ===== place_footprints_grid =============================================== #


def test_place_footprints_grid_distributes_unplaced(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    # Simulate "Update PCB from Schematic" — KiCAD stacks footprints at
    # KiCAD-(0, 0). We add via the editor (which uses MCP coords) then force
    # the underlying `at` to (0, 0) to match the real GUI state.
    for ref in ("R1", "R2", "R3", "C1", "C2"):
        ed.add_footprint(
            tree, qualified_lib_id="MiniFP:Mini_R_0603", reference=ref,
            value="x", x_mm=0, y_mm=0,
            fp_def_node=ed.fetch_footprint_def(
                FIXTURES / "MiniFP.pretty" / "Mini_R_0603.kicad_mod"
            ),
        )
        at = sch_io.find_child(ed.find_footprint_by_reference(tree, ref), "at")
        at[1] = 0.0
        at[2] = 0.0

    summary = ed.place_footprints_grid(tree, spacing_mm=10, columns=3, origin_mcp=(15, 15))
    assert summary["placed"] == 5
    # All footprints should now have unique non-origin positions.
    positions = []
    for fp in ed.iter_footprints(tree):
        at = sch_io.find_child(fp, "at")
        positions.append((float(at[1]), float(at[2])))
    assert len(set(positions)) == 5
    # All footprints now have unique non-origin positions
    positions = []
    for fp in ed.iter_footprints(tree):
        at = sch_io.find_child(fp, "at")
        positions.append((float(at[1]), float(at[2])))
    assert len(set(positions)) == 5


# ===== Tracks / vias ======================================================= #


def test_add_track_persists(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    ed.add_track(tree, 10, 10, 30, 10, width_mm=0.25)
    sch_io.write_file(blank_project["pcb"], tree)
    tree2 = sch_io.parse_file(blank_project["pcb"])
    segs = sch_io.find_children(tree2, "segment")
    assert len(segs) == 1
    assert sch_io.find_child(segs[0], "width")[1] == 0.25


def test_add_via_persists(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    ed.add_via(tree, 20, 15, drill_mm=0.4, diameter_mm=0.8)
    sch_io.write_file(blank_project["pcb"], tree)
    tree2 = sch_io.parse_file(blank_project["pcb"])
    vias = sch_io.find_children(tree2, "via")
    assert len(vias) == 1
    drill = sch_io.find_child(vias[0], "drill")
    assert drill[1] == 0.4


# ===== Tool layer ========================================================== #


def _patched_index(tmp_path):
    """Build an index pointing at the MiniFP fixture."""
    fp_dir = tmp_path / "fps"
    fp_dir.mkdir()
    shutil.copytree(FIXTURES / "MiniFP.pretty", fp_dir / "MiniFP.pretty")
    return kicad_libs.build_index(symbol_dirs=[], footprint_dirs=[fp_dir])


def _make_mcp(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP
    idx = _patched_index(tmp_path)
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)
    mcp = FastMCP("t")
    pcb_tools.register(mcp)
    return mcp


def _call(mcp, name, **kwargs):
    return mcp._tool_manager.get_tool(name).fn(**kwargs)


def test_pcb_tools_end_to_end(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    _call(mcp, "set_board_outline", width_mm=50, height_mm=30)
    _call(mcp, "add_footprint", lib_id="MiniFP:Mini_R_0603",
          reference="R1", value="10k", x_mm=20, y_mm=20)
    _call(mcp, "add_footprint", lib_id="MiniFP:Mini_R_0603",
          reference="R2", value="1k", x_mm=40, y_mm=20)
    _call(mcp, "add_track", x1_mm=20, y1_mm=20, x2_mm=40, y2_mm=20)
    _call(mcp, "add_via", x_mm=30, y_mm=20)

    fps = _call(mcp, "list_footprints")
    assert {fp["reference"] for fp in fps} == {"R1", "R2"}

    # Read schematic back to confirm persistence
    tree = sch_io.parse_file(blank_project["pcb"])
    assert len(sch_io.find_children(tree, "footprint")) == 2
    assert len(sch_io.find_children(tree, "segment")) == 1
    assert len(sch_io.find_children(tree, "via")) == 1


def test_move_footprint_via_tool(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    _call(mcp, "add_footprint", lib_id="MiniFP:Mini_R_0603",
          reference="R1", value="v", x_mm=20, y_mm=20)
    _call(mcp, "move_footprint", reference="R1", x_mm=30, y_mm=25, rotation=180)
    fps = _call(mcp, "list_footprints")
    r1 = next(fp for fp in fps if fp["reference"] == "R1")
    assert r1["position_mm"] == [30.0, 25.0]


# ===== Acceptance: small board with KiCAD official footprints ============== #


@pytest.mark.slow
def test_acceptance_small_board_with_real_libs(tmp_path):
    """Build a 50x30 board with 2 SMD resistors + a track, then run kicad-cli pcb drc."""
    cli = find_kicad_cli()
    if not cli or not find_footprint_lib_dirs():
        pytest.skip("kicad-cli or footprint libs not available")

    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built; run index_libraries first")
    state.clear_active()
    lib_tools._index = cached

    files = write_blank_project(tmp_path / "tinyboard", "tinyboard")
    state.set_active(tmp_path / "tinyboard", "tinyboard")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    pcb_tools.register(mcp)

    _call(mcp, "set_board_outline", width_mm=50, height_mm=30)
    _call(mcp, "add_footprint",
          lib_id="Resistor_SMD:R_0603_1608Metric", reference="R1",
          value="10k", x_mm=20, y_mm=20)
    _call(mcp, "add_footprint",
          lib_id="Resistor_SMD:R_0603_1608Metric", reference="R2",
          value="1k", x_mm=35, y_mm=20)
    _call(mcp, "add_track", x1_mm=20, y1_mm=20, x2_mm=35, y2_mm=20)

    pcb_path = files["pcb"]
    state.clear_active()

    r = subprocess.run(
        [str(cli), "pcb", "drc", str(pcb_path)],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    # DRC will report unconnected/clearance issues but the file MUST parse.
    assert r.returncode == 0, f"drc parse failed: stderr={r.stderr}"


# ===== Float spelling preserved on round-trip ============================== #


def test_number_spelling_survives_round_trip(tmp_path: Path):
    """Untouched numbers are written back exactly as the board spells them.

    KiCAD 10 keeps a negative zero in a 3D model's `(xyz ...)` (`{:.10g}` of
    -0.0 is `-0`, and `int("-0")` would drop the sign), and prints a value
    below 0.0001 as a plain decimal, never an exponent.
    """
    src = (
        "(kicad_pcb\n"
        "\t(version 20260206)\n"
        '\t(generator "pcbnew")\n'
        '\t(generator_version "10.0")\n'
        '\t(footprint "X"\n'
        '\t\t(model "m.wrl"\n'
        "\t\t\t(offset\n"
        "\t\t\t\t(xyz 0 -0 -0)\n"
        "\t\t\t)\n"
        "\t\t\t(scale\n"
        "\t\t\t\t(xyz 1 1 0.00001)\n"
        "\t\t\t)\n"
        "\t\t)\n"
        "\t)\n"
        ")\n"
    )
    pcb = tmp_path / "raw.kicad_pcb"
    pcb.write_text(src, encoding="utf-8", newline="")

    tree = sch_io.parse_file(pcb)
    assert sch_io.dumps(tree) == src


def test_mutated_float_uses_kicad_formatting(tmp_path: Path):
    """A value we replace is spelled by `_format_float`, not by the old token."""
    pcb = tmp_path / "raw.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb\n\t(version 20260206)\n\t(thickness 1.6)\n)\n",
        encoding="utf-8",
        newline="",
    )
    tree = sch_io.parse_file(pcb)
    node = sch_io.find_child(tree, "thickness")
    assert float(node[1]) == 1.6
    node[1] = 0.8
    assert "(thickness 0.8)" in sch_io.dumps(tree)


def test_place_footprints_grid_can_arrange_already_placed_parts(tmp_path: Path):
    """`update_pcb_from_schematic` places new parts below the outline, not at (0,0)."""
    from kicad_claude.adapters import pcb_editor as ed
    from kicad_claude.adapters import sch_io
    from kicad_claude.templates.blank import write_blank_project

    files = write_blank_project(tmp_path / "g", "g")
    tree = sch_io.parse_file(files["pcb"])
    fp_def = [
        sch_io.sym("footprint"), "L:R",
        [sch_io.sym("property"), "Reference", "REF**",
         [sch_io.sym("at"), 0, 0, 0]],
        [sch_io.sym("property"), "Value", "V",
         [sch_io.sym("at"), 0, 0, 0]],
    ]
    for i, ref in enumerate(("R1", "R2")):
        ed.add_footprint(tree, qualified_lib_id="L:R", reference=ref, value="1k",
                         x_mm=60 + i * 5, y_mm=60, fp_def_node=fp_def)

    assert ed.place_footprints_grid(tree)["placed"] == 0
    res = ed.place_footprints_grid(tree, spacing_mm=10, columns=2,
                                   origin_mcp=(20.0, 20.0), only_unplaced=False)
    assert res["placed"] == 2
    positions = {f["reference"]: f["position_mm"]
                 for f in ed.list_footprints_summary(tree)}
    assert positions["R1"] == [20.0, 20.0]
    assert positions["R2"] == [30.0, 20.0]


# ===== Pad angles follow the footprint (defects report 2026-09-20, #1) ===== #


def _extra_fp_def(name: str) -> list:
    return ed.fetch_footprint_def(FIXTURES / "ExtraFP.pretty" / f"{name}.kicad_mod")


def _pad_angles(fp: list) -> list[float]:
    out = []
    for pad in sch_io.find_children(fp, "pad"):
        at = sch_io.find_child(pad, "at")
        out.append(float(at[3]) if len(at) >= 4 else 0.0)
    return out


def test_move_footprint_rotates_pads_with_the_footprint(blank_project):
    """A pad's angle is absolute, so rotating the footprint must rotate it too."""
    tree = sch_io.parse_file(blank_project["pcb"])
    ed.add_footprint(
        tree, qualified_lib_id="ExtraFP:Pads_2P", reference="U1", value="v",
        x_mm=10, y_mm=10, fp_def_node=_extra_fp_def("Pads_2P"),
    )
    fp = ed.find_footprint_by_reference(tree, "U1")
    assert _pad_angles(fp) == [0.0, 45.0]

    ed.move_footprint(tree, "U1", x_mm=10, y_mm=10, rotation=90)
    assert _pad_angles(fp) == [90.0, 135.0]

    # Rotating back takes the pads with it — the delta, not the absolute value.
    ed.move_footprint(tree, "U1", x_mm=10, y_mm=10, rotation=0)
    assert _pad_angles(fp) == [0.0, 45.0]


def test_move_footprint_without_rotation_leaves_pads_alone(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    ed.add_footprint(
        tree, qualified_lib_id="ExtraFP:Pads_2P", reference="U1", value="v",
        x_mm=10, y_mm=10, rotation=90, fp_def_node=_extra_fp_def("Pads_2P"),
    )
    fp = ed.find_footprint_by_reference(tree, "U1")
    assert _pad_angles(fp) == [90.0, 135.0]  # applied at placement time
    ed.move_footprint(tree, "U1", x_mm=40, y_mm=20)
    assert _pad_angles(fp) == [90.0, 135.0]


# ===== KiCad 6 footprints keep their reference (defects report #3) ========= #


def test_legacy_fp_text_footprint_gets_its_reference(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    ed.add_footprint(
        tree, qualified_lib_id="ExtraFP:Legacy_2P", reference="L9", value="100uH",
        x_mm=10, y_mm=10, fp_def_node=_extra_fp_def("Legacy_2P"),
    )
    fp = ed.find_footprint_by_reference(tree, "L9")
    assert fp is not None
    assert ed.get_fp_text(fp, "reference") == "L9"
    assert ed.get_fp_text(fp, "value") == "100uH"
    assert ed.all_footprint_references(tree) == ["L9"]


def test_set_footprint_property_writes_legacy_fp_text(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    ed.add_footprint(
        tree, qualified_lib_id="ExtraFP:Legacy_2P", reference="L9", value="100uH",
        x_mm=10, y_mm=10, fp_def_node=_extra_fp_def("Legacy_2P"),
    )
    fp = ed.find_footprint_by_reference(tree, "L9")
    assert ed.set_footprint_property(fp, "Value", "220uH") is True
    assert ed.get_fp_text(fp, "value") == "220uH"
    # No second, conflicting field was added.
    assert sch_io.get_property(fp, "Value") is None
    assert ed.set_footprint_property(fp, "Value", "220uH") is False


@pytest.mark.slow
def test_acceptance_rotating_a_real_footprint_keeps_drc_clean(tmp_path):
    """#1 — a rotated USB-C used to short its own lands (54 extra violations).

    The check is rotation invariance: the same footprint must produce the same
    DRC errors at 0° and at 90°. The handful it produces at 0° belong to the
    library footprint against the blank project's default rules.
    """
    from kicad_claude.adapters import kicad_cli

    cli = find_kicad_cli()
    fp_dirs = find_footprint_lib_dirs()
    if not cli or not fp_dirs:
        pytest.skip("kicad-cli or footprint libs not available")
    mod = next(
        (d / "Connector_USB.pretty" /
         "USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.kicad_mod"
         for d in fp_dirs
         if (d / "Connector_USB.pretty").is_dir()),
        None,
    )
    if mod is None or not mod.is_file():
        pytest.skip("Connector_USB library not available")

    counts = {}
    for rotation in (0, 90):
        files = write_blank_project(tmp_path / f"rot{rotation}", "rot")
        tree = sch_io.parse_file(files["pcb"])
        ed.set_board_outline(tree, 40, 40)
        ed.add_footprint(
            tree,
            qualified_lib_id=(
                "Connector_USB:"
                "USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"
            ),
            reference="J1", value="USB_C", x_mm=20, y_mm=20,
            fp_def_node=ed.fetch_footprint_def(mod),
        )
        ed.move_footprint(tree, "J1", x_mm=20, y_mm=20, rotation=rotation)
        sch_io.write_file(files["pcb"], tree)
        report = kicad_cli.run_drc(files["pcb"], schematic_parity=False)
        counts[rotation] = report["errors"]
        types = {v["type"] for v in report["violations"]}
        assert "shorting_items" not in types

    assert counts[90] == counts[0]


# ===== remove_board_items (v2.0) =========================================== #


_ROUTING = """(kicad_pcb
  (segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net "GND") (uuid "s1"))
  (segment (start 20 10) (end 20 20) (width 0.25) (layer "B.Cu") (net "GND") (uuid "s2"))
  (via (at 20 10) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "GND") (uuid "v1"))
  (segment (start 10 30) (end 20 30) (width 0.25) (layer "F.Cu") (net "VCC") (uuid "s3"))
)"""


def _routed_board(path):
    """Tracks and a via in KiCAD 10's net-by-name form, on nets no pad has."""
    tree = sch_io.parse_file(path)
    tree.extend(sexpdata.loads(_ROUTING)[1:])
    sch_io.write_file(path, tree)


def test_remove_board_items_rips_up_a_net(blank_project, tmp_path, monkeypatch):
    _routed_board(blank_project["pcb"])
    mcp = _make_mcp(monkeypatch, tmp_path)
    dry = _call(mcp, "remove_board_items", net="GND", dry_run=True)
    assert dry["by_kind"] == {"track": 2, "via": 1}
    assert len(sch_io.find_children(sch_io.parse_file(blank_project["pcb"]), "segment")) == 3

    res = _call(mcp, "remove_board_items", net="GND")
    assert res["removed_count"] == 3 and res["backup"]
    tree = sch_io.parse_file(blank_project["pcb"])
    assert [ed.net_of(s) for s in sch_io.find_children(tree, "segment")] == ["VCC"]
    assert sch_io.find_children(tree, "via") == []


def test_remove_board_items_one_layer_spares_vias(blank_project, tmp_path, monkeypatch):
    _routed_board(blank_project["pcb"])
    mcp = _make_mcp(monkeypatch, tmp_path)
    res = _call(mcp, "remove_board_items", net="GND", layer="B.Cu")
    assert res["by_kind"] == {"track": 1}
    tree = sch_io.parse_file(blank_project["pcb"])
    assert len(sch_io.find_children(tree, "via")) == 1


def test_remove_board_items_by_uuid(blank_project, tmp_path, monkeypatch):
    _routed_board(blank_project["pcb"])
    via = sch_io.find_children(sch_io.parse_file(blank_project["pcb"]), "via")[0]
    uid = str(sch_io.find_child(via, "uuid")[1])
    mcp = _make_mcp(monkeypatch, tmp_path)
    res = _call(mcp, "remove_board_items", uuid=uid)
    assert res["items"] == [{"kind": "via", "uuid": uid, "net": "GND",
                             "layers": ["F.Cu", "B.Cu"]}]
    assert sch_io.find_children(sch_io.parse_file(blank_project["pcb"]), "via") == []


def test_remove_board_items_refuses_without_target(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="uuid or a net"):
        _call(mcp, "remove_board_items")
    with pytest.raises(KeyError, match="no track"):
        _call(mcp, "remove_board_items", uuid="nope")
    with pytest.raises(ValueError, match="unknown kind"):
        _call(mcp, "remove_board_items", net="GND", kinds=["pad"])


def test_add_track_and_via_take_a_net_name(blank_project, tmp_path, monkeypatch):
    """OP-18: the tools pass a net name through; KiCAD 10 stores it by name."""
    _routed_board(blank_project["pcb"])
    mcp = _make_mcp(monkeypatch, tmp_path)
    _call(mcp, "add_track", x1_mm=30, y1_mm=10, x2_mm=40, y2_mm=10, net="GND")
    _call(mcp, "add_via", x_mm=40, y_mm=10, net="GND")
    tree = sch_io.parse_file(blank_project["pcb"])
    assert [ed.net_of(s) for s in sch_io.find_children(tree, "segment")].count("GND") == 3
    assert [ed.net_of(v) for v in sch_io.find_children(tree, "via")] == ["GND", "GND"]
    seg = sch_io.find_children(tree, "segment")[-1]
    assert sch_io.find_child(seg, "net")[1] == "GND"


def test_add_track_refuses_unknown_net_and_index_on_kicad10(blank_project, tmp_path, monkeypatch):
    _routed_board(blank_project["pcb"])
    mcp = _make_mcp(monkeypatch, tmp_path)
    with pytest.raises(KeyError, match="not found"):
        _call(mcp, "add_track", x1_mm=0, y1_mm=0, x2_mm=1, y2_mm=0, net="NOPE")
    with pytest.raises(KeyError, match="pass the net name"):
        _call(mcp, "add_via", x_mm=1, y_mm=1, net=3)
    tree = sch_io.parse_file(blank_project["pcb"])
    assert len(sch_io.find_children(tree, "segment")) == 3


# ===== OP-21: footprint zones live in board coordinates ==================== #

_FP_WITH_ZONE = """(footprint "Z"
  (layer "F.Cu")
  (property "Reference" "REF**" (at 0 0) (layer "F.SilkS"))
  (property "Value" "Z" (at 0 1) (layer "F.Fab"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  (zone (layers "F.Cu" "B.Cu") (uuid "z1") (name "keepout")
    (keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed)
             (copperpour not_allowed) (footprints allowed))
    (polygon (pts (xy 1 -2) (xy 3 -2) (xy 3 2) (xy 1 2))))
)"""


def _zone_pts(fp):
    zone = sch_io.find_child(fp, "zone")
    pts = sch_io.find_child(sch_io.find_child(zone, "polygon"), "pts")
    return [(float(p[1]), float(p[2])) for p in sch_io.find_children(pts, "xy")]


def test_footprint_zone_follows_place_and_move(blank_project):
    tree = sch_io.parse_file(blank_project["pcb"])
    fp_def = sexpdata.loads(_FP_WITH_ZONE)
    fp = ed.add_footprint(tree, qualified_lib_id="T:Z", reference="U1", value="z",
                          x_mm=40, y_mm=30, fp_def_node=fp_def)
    assert _zone_pts(fp)[0] == (41.0, 28.0)
    assert sch_io.find_child(fp, "at")[1:] == [40, 30]  # KiCAD omits angle 0
    ed.move_footprint(tree, "U1", 10, 10)
    assert _zone_pts(fp)[0] == (11.0, 8.0)
    # 90 deg CCW on a Y-down axis: local (1, -2) -> (-2, -1)
    ed.move_footprint(tree, "U1", 10, 10, rotation=90)
    assert _zone_pts(fp)[0] == pytest.approx((8.0, 9.0))
    ed.place_footprints_grid(tree, only_unplaced=False, origin_mcp=(50, 50))
    assert _zone_pts(fp)[0] == pytest.approx((48.0, 49.0))
    # The library copy itself stays footprint-local.
    assert sch_io.find_children(sch_io.find_child(sch_io.find_child(
        sch_io.find_child(fp_def, "zone"), "polygon"), "pts"), "xy")[0][1:] == [1, -2]


def test_set_footprint_attr_flags_keeps_kicad_order(blank_project):
    fp = sexpdata.loads('(footprint "X" (layer "F.Cu") (attr smd exclude_from_bom) (pad "1" smd rect))')
    assert ed.set_footprint_attr_flags(fp, {"exclude_from_bom": False, "dnp": True}) == [
        "exclude_from_bom", "dnp"]
    assert [str(t) for t in sch_io.find_child(fp, "attr")[1:]] == ["smd", "dnp"]
    assert ed.set_footprint_attr_flags(fp, {"dnp": True}) == []
    fp2 = sexpdata.loads('(footprint "X" (layer "F.Cu") (property "Reference" "R") (pad "1" smd rect))')
    ed.set_footprint_attr_flags(fp2, {"exclude_from_bom": True})
    heads = [str(c[0]) for c in fp2[2:]]
    assert heads == ["layer", "property", "attr", "pad"]
    ed.set_footprint_attr_flags(fp2, {"exclude_from_bom": False})
    assert sch_io.find_child(fp2, "attr") is None


@pytest.mark.slow
def test_acceptance_footprint_with_keepout_matches_library(tmp_path):
    """ESP32-C6-MINI-1 carries an antenna keep-out zone: no lib mismatch."""
    import json
    cli = find_kicad_cli()
    mod = Path(r"C:\Program Files\KiCad\10.0\share\kicad\footprints\RF_Module.pretty"
               r"\ESP32-C6-MINI-1.kicad_mod")
    if cli is None or not mod.is_file():
        pytest.skip("kicad-cli / RF_Module library not found")
    files = write_blank_project(tmp_path / "k", "k")
    tree = sch_io.parse_file(files["pcb"])
    ed.set_board_outline(tree, 80, 80)
    ed.add_footprint(tree, qualified_lib_id="RF_Module:ESP32-C6-MINI-1", reference="U1",
                     value="x", x_mm=40, y_mm=40, fp_def_node=ed.fetch_footprint_def(mod))
    ed.move_footprint(tree, "U1", 45, 42, rotation=90)
    sch_io.write_file(files["pcb"], tree)
    rep = tmp_path / "drc.json"
    subprocess.run([str(cli), "pcb", "drc", "--format", "json", "-o", str(rep), str(files["pcb"])],
                   stdin=subprocess.DEVNULL, capture_output=True, check=False)
    kinds = [v["type"] for v in json.loads(rep.read_text(encoding="utf-8"))["violations"]]
    assert "lib_footprint_mismatch" not in kinds


# ===== OP-22: edit_board, courtyard and pad size =========================== #

def _mcp_with_batch(monkeypatch, tmp_path):
    from kicad_claude.tools.pcb import batch as batch_tools
    mcp = _make_mcp(monkeypatch, tmp_path)
    batch_tools.register(mcp)
    return mcp


def _two_resistors(mcp):
    _call(mcp, "add_footprint", lib_id="MiniFP:Mini_R_0603", reference="R1",
          value="1k", x_mm=20, y_mm=20)
    _call(mcp, "add_footprint", lib_id="MiniFP:Mini_R_0603", reference="R2",
          value="1k", x_mm=40, y_mm=20)


def test_edit_board_moves_and_routes_in_one_write(blank_project, tmp_path, monkeypatch):
    mcp = _mcp_with_batch(monkeypatch, tmp_path)
    _two_resistors(mcp)
    _routed_board(blank_project["pcb"])   # nets GND / VCC exist on the board
    res = _call(mcp, "edit_board", ops=[
        {"op": "move", "ref": "R2", "x_mm": 30, "y_mm": 25, "rotation": 90},
        {"op": "track", "from": [20, 20], "points": [[25, 20]], "to": [25, 25],
         "net": "GND", "width_mm": 0.4},
        {"op": "via", "x_mm": 25, "y_mm": 25, "net": "GND"},
        {"op": "zone", "net": "VCC", "layer": "B.Cu",
         "polygon_mm": [[10, 10], [50, 10], [50, 40]], "pad_connection": "solid", "priority": 1},
        {"op": "remove", "net": "VCC", "kinds": ["track"]},
    ])
    assert res["applied"] == {"move": 1, "track": 1, "via": 1, "zone": 1, "remove": 1}
    assert res["notes"] == [{"i": 4, "removed": 1}]
    tree = sch_io.parse_file(blank_project["pcb"])
    fps = {f["reference"]: f for f in ed.list_footprints_summary(tree)}
    assert fps["R2"]["position_mm"] == [30, 25] and fps["R2"]["rotation"] == 90
    segs = [s for s in sch_io.find_children(tree, "segment") if ed.net_of(s) == "GND"]
    assert len(segs) == 4  # 2 from the fixture + 2 new
    assert [ed.net_of(v) for v in sch_io.find_children(tree, "via")] == ["GND", "GND"]
    assert [ed.net_of(s) for s in sch_io.find_children(tree, "segment")].count("VCC") == 0


def test_edit_board_is_all_or_nothing(blank_project, tmp_path, monkeypatch):
    mcp = _mcp_with_batch(monkeypatch, tmp_path)
    _two_resistors(mcp)
    before = blank_project["pcb"].read_text(encoding="utf-8")
    with pytest.raises(ValueError, match=r"op 1 \(track\).*nothing written"):
        _call(mcp, "edit_board", ops=[
            {"op": "move", "ref": "R1", "x_mm": 5, "y_mm": 5},
            {"op": "track", "from": "R1.1", "to": "R9.1"},
        ])
    assert blank_project["pcb"].read_text(encoding="utf-8") == before
    dry = _call(mcp, "edit_board", ops=[{"op": "move", "ref": "R1", "x_mm": 5, "y_mm": 5}],
                dry_run=True)
    assert dry["dry_run"] and blank_project["pcb"].read_text(encoding="utf-8") == before


_R_DEF = """(footprint "R" (layer "F.Cu")
  (property "Reference" "REF**" (at 0 -1.4) (layer "F.SilkS"))
  (property "Value" "R" (at 0 1.4) (layer "F.Fab"))
  (fp_rect (start -1.5 -0.75) (end 1.5 0.75) (layer "F.CrtYd"))
  (pad "1" smd rect (at -0.8 0) (size 0.8 0.9) (layers "F.Cu"))
  (pad "2" smd rect (at 0.8 0) (size 0.8 0.9) (layers "F.Cu"))
)"""


def _pads_on_sig(path):
    tree = sch_io.parse_file(path)
    for ref, x in (("R1", 20), ("R2", 40)):
        ed.add_footprint(tree, qualified_lib_id="T:R", reference=ref, value="1k",
                         x_mm=x, y_mm=20, fp_def_node=sexpdata.loads(_R_DEF))
    for fp in ed.iter_footprints(tree):
        for pad in sch_io.find_children(fp, "pad"):
            pad.append([sexpdata.Symbol("net"), "SIG"])
    sch_io.write_file(path, tree)


def test_edit_board_track_between_pads_takes_their_net(blank_project, tmp_path, monkeypatch):
    mcp = _mcp_with_batch(monkeypatch, tmp_path)
    _pads_on_sig(blank_project["pcb"])
    res = _call(mcp, "edit_board", ops=[
        {"op": "track", "from": "R1.2", "to": "R2.1"},
        {"op": "via", "at": "R2.2"}])
    assert res["notes"] == [{"i": 0, "net": "SIG"}]
    tree = sch_io.parse_file(blank_project["pcb"])
    seg = sch_io.find_children(tree, "segment")[0]
    assert ed.net_of(seg) == "SIG"
    assert [float(v) for v in sch_io.find_child(seg, "start")[1:]] == [20.8, 20.0]
    assert ed.net_of(sch_io.find_children(tree, "via")[0]) == "SIG"


def test_list_footprints_courtyard_and_list_pads_size(blank_project, tmp_path, monkeypatch):
    mcp = _make_mcp(monkeypatch, tmp_path)
    _pads_on_sig(blank_project["pcb"])
    fp = {f["reference"]: f for f in _call(mcp, "list_footprints")}["R1"]
    assert fp["courtyard_mm"] == [18.5, 19.25, 21.5, 20.75]
    pads = _call(mcp, "list_pads", reference="R1")
    assert [p["size_mm"] for p in pads] == [[0.8, 0.9], [0.8, 0.9]]
    assert pads[0]["shape"] == "rect"
    _call(mcp, "move_footprint", reference="R1", x_mm=20, y_mm=20, rotation=90)
    fp = {f["reference"]: f for f in _call(mcp, "list_footprints")}["R1"]
    assert fp["courtyard_mm"] == [19.25, 18.5, 20.75, 21.5]


def test_courtyard_bbox_rotates_with_the_footprint():
    fp = sexpdata.loads('(footprint "X" (layer "F.Cu") (at 10 10 90)'
                        ' (fp_rect (start -2 -1) (end 2 1) (layer "F.CrtYd")))')
    assert ed.footprint_courtyard_bbox(fp) == [9, 8, 11, 12]
    fp = sexpdata.loads('(footprint "X" (layer "F.Cu") (at 0 0)'
                        ' (fp_circle (center 1 1) (end 2 1) (layer "F.CrtYd")))')
    assert ed.footprint_courtyard_bbox(fp) == [0, 0, 2, 2]


# ===== Field test 3 (OP-37 / OP-38 / OP-39) ================================ #


def _pcb_tree(blank_project):
    return sch_io.parse_file(blank_project["pcb"])


def test_back_silk_text_is_mirrored_and_text_ops_edit_and_remove(blank_project, tmp_path,
                                                                  monkeypatch):
    mcp = _mcp_with_batch(monkeypatch, tmp_path)
    res = _call(mcp, "add_silk_text", text="REV A", x_mm=10, y_mm=10, layer="B.SilkS")
    node = sch_io.find_children(_pcb_tree(blank_project), "gr_text")[0]
    assert res["uuid"] == str(sch_io.find_child(node, "uuid")[1])
    j = sch_io.find_child(sch_io.find_child(node, "effects"), "justify")
    assert [str(w) for w in j[1:]] == ["mirror"]

    res = _call(mcp, "edit_board", ops=[
        {"op": "text", "uuid": res["uuid"], "text": "REV B", "x_mm": 12, "layer": "F.SilkS"},
        {"op": "text", "text": "T2", "x_mm": 5, "y_mm": 5}])
    new_uuid = res["notes"][0]["uuid"]
    texts = sch_io.find_children(_pcb_tree(blank_project), "gr_text")
    first = texts[0]
    assert first[1] == "REV B"
    assert [float(v) for v in sch_io.find_child(first, "at")[1:3]] == [12.0, 10.0]
    assert sch_io.find_child(sch_io.find_child(first, "effects"), "justify") is None
    _call(mcp, "edit_board", ops=[{"op": "remove", "uuid": new_uuid}])
    assert len(sch_io.find_children(_pcb_tree(blank_project), "gr_text")) == 1


def test_zone_set_changes_a_pour_in_place(blank_project, tmp_path, monkeypatch):
    mcp = _mcp_with_batch(monkeypatch, tmp_path)
    _call(mcp, "edit_board", ops=[
        {"op": "zone", "net": "", "layer": "F.Cu", "name": "POUR",
         "polygon_mm": [[0, 0], [10, 0], [10, 10]]}])
    zone = sch_io.find_children(_pcb_tree(blank_project), "zone")[0]
    zid = str(sch_io.find_child(zone, "uuid")[1])
    _call(mcp, "edit_board", ops=[{"op": "zone_set", "uuid": zid, "pad_connection": "solid",
                                   "priority": 2, "clearance_mm": 0.3}])
    zone = sch_io.find_children(_pcb_tree(blank_project), "zone")[0]
    cp = sch_io.find_child(zone, "connect_pads")
    assert str(cp[1]) == "yes"
    assert float(sch_io.find_child(cp, "clearance")[1]) == 0.3
    assert int(sch_io.find_child(zone, "priority")[1]) == 2
    assert str(sch_io.find_child(zone, "uuid")[1]) == zid
    _call(mcp, "edit_board", ops=[{"op": "zone_set", "name": "POUR",
                                   "pad_connection": "thermal"}])
    cp = sch_io.find_child(sch_io.find_children(_pcb_tree(blank_project), "zone")[0], "connect_pads")
    assert all(isinstance(c, list) for c in cp[1:])


def test_move_near_packs_parts_beside_the_anchor_by_courtyard(blank_project, tmp_path,
                                                               monkeypatch):
    mcp = _mcp_with_batch(monkeypatch, tmp_path)
    tree = _pcb_tree(blank_project)
    for ref, x in (("U1", 50), ("C1", 10), ("C2", 10)):
        ed.add_footprint(tree, qualified_lib_id="T:R", reference=ref, value="x",
                         x_mm=x, y_mm=x, fp_def_node=sexpdata.loads(_R_DEF))
    sch_io.write_file(blank_project["pcb"], tree)
    # U1 courtyard [48.5, 49.25, 51.5, 50.75]; a rotated R courtyard is 1.5 x 3.
    res = _call(mcp, "edit_board", ops=[
        {"op": "move", "refs": ["C1", "C2"], "near": "U1.1", "side": "left",
         "rotation": 90, "gap_mm": 0.5}])
    placed = {p["ref"]: p for p in res["notes"][0]["placed"]}
    boxes = {f["reference"]: f["courtyard_mm"]
             for f in _call(mcp, "list_footprints")}
    assert boxes["C1"][2] == 48.0          # right edge 0.5 mm left of U1
    assert boxes["C1"][1] + boxes["C1"][3] == 2 * 50  # centred on U1.1 (y 50)
    assert boxes["C2"][1] == boxes["C1"][3] + 0.5   # stacked below
    assert placed["C2"]["x_mm"] == placed["C1"]["x_mm"]


def test_thin_reference_is_raised_to_the_text_minimum():
    from kicad_claude.adapters import pcb_fields as pf
    from tests.test_pcb_fields import _board, _fp

    tree = _board(_fp("Q2", 20, 20).replace("(thickness 0.15)", "(thickness 0.1)", 1))
    fp = ed.find_footprint_by_reference(tree, "Q2")
    r = pf.tidy_fields(tree, min_thickness=0.15, min_height=0.8)
    assert r["raised_to_minimum"] == ["Q2"]
    assert pf.field_info(fp, pf.field_node(fp, "Reference"))["thickness_mm"] == 0.15
    assert pf.set_field(fp, "Reference", thickness=0.2)
    assert pf.field_info(fp, pf.field_node(fp, "Reference"))["thickness_mm"] == 0.2


def test_connector_edge_side_body_and_pad_bbox(blank_project, tmp_path, monkeypatch):
    import pytest

    from kicad_claude.utils.kicad_paths import find_footprint_lib_dirs

    mod = next((d / "Connector_USB.pretty" /
                "USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.kicad_mod"
                for d in map(__import__("pathlib").Path, find_footprint_lib_dirs())
                if (d / "Connector_USB.pretty").is_dir()), None)
    if mod is None or not mod.is_file():
        pytest.skip("KiCAD footprint libraries not installed")
    mcp = _mcp_with_batch(monkeypatch, tmp_path)
    tree = _pcb_tree(blank_project)
    ed.add_footprint(tree, qualified_lib_id="Connector_USB:USB4105", reference="J4",
                     value="USB", x_mm=30, y_mm=30, rotation=90,
                     fp_def_node=ed.fetch_footprint_def(mod))
    sch_io.write_file(blank_project["pcb"], tree)
    j4 = {f["reference"]: f for f in _call(mcp, "list_footprints")}["J4"]
    assert j4["edge_side"] == "right"     # mouth at local +y, turned 90 deg CCW
    assert j4["body_mm"] == [26.325, 25.53, 33.675, 34.47]
    pad = _call(mcp, "list_pads", reference="J4")[0]
    w, h = pad["size_mm"]
    box = pad["bbox_mm"]
    assert round(box[2] - box[0], 3) == h and round(box[3] - box[1], 3) == w  # rotated


def test_power_path_block_is_reported():
    from kicad_claude.adapters import assembly_review as ar

    def fp(ref, x, pads):
        hull = [(x - 1, -1), (x + 1, -1), (x + 1, 1), (x - 1, 1)]
        return {"ref": ref, "attrs": set(), "hull": hull,
                "pads": [{"net": n, "at": (x, 0)} for n in pads]}

    fps = [fp("J1", 0, ["/VIN"]), fp("L1", 10, ["SW"]), fp("Q2", 20, ["/VIN"]),
           fp("R1", 30, ["SIG"])]
    assert ar.power_path_blocks(fps) == [{"net": "/VIN", "ref": "L1", "between": ["J1", "Q2"]}]
    fps[1]["pads"].append({"net": "/VIN", "at": (10, 0)})
    assert ar.power_path_blocks(fps) == []


def test_stacked_pads_of_one_net_are_connected():
    """USB-C A4 / B9 share one land: DRC sees no missing link there."""
    from kicad_claude.adapters import pcb_netlist

    tree = sexpdata.loads("""(kicad_pcb (version 20260206)
      (footprint "T:J" (layer "F.Cu") (at 10 10)
        (property "Reference" "J4" (at 0 0) (layer "F.SilkS"))
        (pad "A4" smd rect (at 0 0) (size 0.6 1.2) (layers "F.Cu") (net "VBUS"))
        (pad "B9" smd rect (at 0 0) (size 0.6 1.2) (layers "F.Cu") (net "VBUS"))
        (pad "A9" smd rect (at 5 0) (size 0.6 1.2) (layers "F.Cu") (net "VBUS"))))""")
    r = pcb_netlist.list_unrouted(tree)
    assert r["count"] == 1  # A4/B9 joined; only the link to A9 is missing
