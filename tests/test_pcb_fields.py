"""Footprint fields on the board (OP-31) and footprint rotation range (OP-32)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import sexpdata

from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import pcb_fields as pf
from kicad_claude.adapters import sch_io
from kicad_claude.utils.kicad_paths import find_footprint_lib_dirs, find_kicad_cli


def _fp(ref: str, x: float, y: float, rot: float = 0, value_layer: str = "F.SilkS") -> str:
    return f"""
  (footprint "T:R" (layer "F.Cu") (uuid "u-{ref}") (at {x} {y} {rot})
    (property "Reference" "{ref}" (at 0 0 {rot}) (layer "F.SilkS") (uuid "r-{ref}")
      (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "10k" (at 0 1.5 {rot}) (layer "{value_layer}") (uuid "v-{ref}")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -1.5 -0.8) (end 1.5 0.8) (stroke (width 0.05) (type solid)) (fill no)
      (layer "F.CrtYd") (uuid "c-{ref}"))
    (pad "1" smd roundrect (at -0.8 0 {rot}) (size 0.9 1) (layers "F.Cu" "F.Mask" "F.Paste")
      (roundrect_rratio 0.25) (uuid "p1-{ref}"))
    (pad "2" smd roundrect (at 0.8 0 {rot}) (size 0.9 1) (layers "F.Cu" "F.Mask" "F.Paste")
      (roundrect_rratio 0.25) (uuid "p2-{ref}")))"""


def _board(*fps: str) -> list:
    text = ("(kicad_pcb (version 20260206) (generator \"pcbnew\")\n"
            "  (gr_rect (start 0 0) (end 50 40) (stroke (width 0.05) (type default))"
            " (fill no) (layer \"Edge.Cuts\") (uuid \"edge\"))"
            + "".join(fps) + ")")
    return sexpdata.loads(text)


def test_field_position_is_written_in_the_footprint_frame():
    tree = _board(_fp("R1", 10, 10, 90))
    fp = ed.find_footprint_by_reference(tree, "R1")
    assert pf.set_field(fp, "Reference", position=(12, 10), angle=0)
    at = sch_io.find_child(pf.field_node(fp, "Reference"), "at")
    assert (at[1], at[2], at[3]) == (0, 2, 0)          # rotation taken out, angle absolute
    info = pf.field_info(fp, pf.field_node(fp, "Reference"))
    assert info["position_mm"] == [12, 10] and info["angle"] == 0


def test_field_visibility_and_size():
    tree = _board(_fp("R1", 10, 10))
    fp = ed.find_footprint_by_reference(tree, "R1")
    assert pf.set_field(fp, "Value", visible=False)
    prop = pf.field_node(fp, "Value")
    heads = [sch_io.head_of(c) for c in prop if isinstance(c, list)]
    assert heads.index("hide") == heads.index("layer") + 1    # KiCAD's order
    assert not pf.set_field(fp, "Value", visible=False)       # no-op
    assert pf.set_field(fp, "Value", visible=True)
    assert sch_io.find_child(prop, "hide") is None
    pf.set_field(fp, "Reference", size=0.8)
    info = pf.field_info(fp, pf.field_node(fp, "Reference"))
    assert info["size_mm"] == [0.8, 0.8] and info["thickness_mm"] == pytest.approx(0.12)
    with pytest.raises(KeyError):
        pf.set_field(fp, "MPN", visible=True)


def test_vertical_text_box_is_tall():
    b = pf.text_box("R10", (0, 0), 90, (1, 1), 0.15, set(), False)
    assert b[3] - b[1] > b[2] - b[0]
    left = pf.text_box("R10", (0, 0), 90, (1, 1), 0.15, {"left"}, False)
    assert left[3] <= 0.01                              # 90: text runs up from the anchor
    left = pf.text_box("R10", (0, 0), 270, (1, 1), 0.15, {"left"}, False)
    assert left[1] >= -0.01


def test_tidy_moves_references_off_pads_and_hides_silk_values():
    tree = _board(_fp("R1", 10, 10), _fp("R2", 10, 13), _fp("R3", 30, 20, 90, "F.Fab"))
    result = pf.tidy_fields(tree)
    assert {m["ref"] for m in result["moved"]} == {"R1", "R2", "R3"}
    assert set(result["values_hidden"]) == {"R1", "R2"}           # R3's Value is on F.Fab
    pads = [p for p in __import__("kicad_claude.adapters.pcb_netlist",
                                  fromlist=["x"]).list_pads(tree)]
    boxes = []
    for fp in ed.iter_footprints(tree):
        info = pf.field_info(fp, pf.field_node(fp, "Reference"))
        box = info["box_mm"]
        for p in pads:
            xs, ys = [c[0] for c in p["corners"]], [c[1] for c in p["corners"]]
            assert not pf._overlap(box, [min(xs), min(ys), max(xs), max(ys)]), info
        assert all(not pf._overlap(box, b) for b in boxes)
        assert 0 <= box[0] and box[2] <= 50 and 0 <= box[1] and box[3] <= 40
        boxes.append(box)


def test_tidy_reports_a_part_with_no_room():
    tree = _board(_fp("R1", 25, 20))       # outline shrunk to the courtyard
    edge = next(c for c in tree if sch_io.is_call(c, "gr_rect"))
    sch_io.find_child(edge, "start")[1:] = [23.4, 19.1]
    sch_io.find_child(edge, "end")[1:] = [26.6, 20.9]
    r = pf.tidy_fields(tree)
    assert r["unplaced"] == ["R1"] and not r["moved"]
    r = pf.tidy_fields(tree, hide_unplaced=True)
    assert r["references_hidden"] == ["R1"]


def test_list_footprints_reports_rotation_in_0_360():
    tree = _board(_fp("R1", 10, 10, -90), _fp("R2", 20, 10, -180))
    rot = {f["reference"]: f["rotation"] for f in ed.list_footprints_summary(tree)}
    assert rot == {"R1": 270.0, "R2": 180.0}


@pytest.mark.slow
def test_acceptance_tidy_clears_silk_drc(tmp_path: Path):
    """Packed 0603s with their library text: tidy leaves no silk_over_copper /
    silk_overlap in kicad-cli DRC."""
    from kicad_claude.adapters import kicad_cli
    from kicad_claude.templates.blank import write_blank_project

    cli, fp_dirs = find_kicad_cli(), find_footprint_lib_dirs()
    mod = next((d / "Resistor_SMD.pretty" / "R_0603_1608Metric.kicad_mod" for d in fp_dirs
                if (d / "Resistor_SMD.pretty").is_dir()), None) if fp_dirs else None
    if not cli or mod is None or not mod.is_file():
        pytest.skip("kicad-cli or footprint libs not available")
    files = write_blank_project(tmp_path / "silk", "silk")
    tree = sch_io.parse_file(files["pcb"])
    ed.set_board_outline(tree, 30, 20)
    for i in range(4):
        for j in range(3):
            ed.add_footprint(tree, qualified_lib_id="Resistor_SMD:R_0603_1608Metric",
                             reference=f"R{i * 3 + j + 1}", value="10k",
                             x_mm=16 + 2.6 * i, y_mm=17 + 2.2 * j,
                             rotation=90 if j == 1 else 0,
                             fp_def_node=ed.fetch_footprint_def(mod))
    sch_io.write_file(files["pcb"], tree)
    silk = {"silk_over_copper", "silk_overlap"}

    def count():
        rep = kicad_cli.run_drc(files["pcb"], schematic_parity=False)
        return sum(1 for v in rep["violations"] if v["type"] in silk)

    before = count()
    tree = sch_io.parse_file(files["pcb"])
    r = pf.tidy_fields(tree, size=0.8, hide_unplaced=True)
    sch_io.write_file(files["pcb"], tree)
    after = count()
    print("silk DRC before/after:", before, after, r)
    assert before > 0 and after == 0, (before, after, r["unplaced"])
    shutil.rmtree(tmp_path / "silk", ignore_errors=True)
