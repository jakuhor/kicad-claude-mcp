"""Pin positions of rotated + mirrored symbols (OP-33).

KiCAD composes the mirror after the rotation (SCH_SYMBOL::SetOrientation);
at 90 / 270 that differs from mirror-then-rotate. Field case: Q2
`Q_PMOS_SGD` at (219.71 29.21 270) (mirror x).
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_geometry, sch_io
from kicad_claude.adapters import sch_netlist as netlist
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.utils.kicad_paths import find_kicad_cli, find_symbol_lib_dirs

# Q_PMOS_SGD's pins as KiCAD's Transistor_FET library draws them (Y up).
_LIB = """(kicad_symbol_lib (version 20251024) (generator "kicad_symbol_editor")
  (symbol "Q_PMOS_SGD" (pin_names (offset 0) hide) (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "Q" (at 5.08 1.27 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Q_PMOS_SGD" (at 5.08 -1.27 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Description" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (symbol "Q_PMOS_SGD_1_1"
      (pin passive line (at 2.54 5.08 270) (length 2.54) (name "S" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin input line (at -5.08 0 0) (length 2.54) (name "G" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 2.54 -5.08 90) (length 2.54) (name "D" (effects (font (size 1.27 1.27))))
        (number "3" (effects (font (size 1.27 1.27))))))))
"""


def _place(tmp_path: Path, lib: Path, name: str, lib_id: str, placements) -> tuple[dict, list]:
    files = write_blank_project(tmp_path / "rm", "rm")
    tree = sch_io.parse_file(files["sch"])
    sym_def = ed.fetch_symbol_def(lib, name)
    for ref, x, y, rot, mirror in placements:
        node = ed.add_symbol(tree, qualified_lib_id=lib_id, reference=ref, value=name,
                             x_mm=x, y_mm=y, rotation=rot, sym_def_node=sym_def,
                             project_name="rm")
        ed.set_symbol_mirror(node, mirror)
    return files, tree


def test_field_case_q2_270_mirror_x(tmp_path):
    lib = tmp_path / "Q.kicad_sym"
    lib.write_text(_LIB, encoding="utf-8")
    _, tree = _place(tmp_path, lib, "Q_PMOS_SGD", "Q:Q_PMOS_SGD",
                     [("Q2", 219.71, 29.21, 270, "x")])
    want = {"1": [224.79, 26.67], "2": [219.71, 34.29], "3": [214.63, 26.67]}
    got = {p["number"]: p["position_mm"] for p in ed.list_pins_for_symbol(tree, "Q2")}
    assert got == want
    node = ed.find_symbol_by_reference(tree, "Q2")
    got = {p["number"]: list(p["point"]) for p in netlist.pins_of_instance(tree, node)}
    assert got == want
    frame = sch_geometry.Frame(node)
    assert list(frame.point(-5.08, 0)) == want["2"]


@pytest.mark.slow
def test_all_rotations_and_mirrors_match_kicad_cli(tmp_path):
    """12 placements of Transistor_FET:Q_PMOS_SGD, a label on each computed pin
    point; KiCAD's netlist must put every pin on its own label."""
    cli = find_kicad_cli()
    lib = next((d / "Transistor_FET.kicad_sym" for d in find_symbol_lib_dirs()
                if (d / "Transistor_FET.kicad_sym").is_file()), None)
    if not cli or lib is None:
        pytest.skip("kicad-cli or Transistor_FET library not available")
    placements, n = [], 0
    for rot in (0, 90, 180, 270):
        for mirror in ("", "x", "y"):
            n += 1
            placements.append((f"Q{n}", 25.4 * (1 + len(placements) % 4),
                               25.4 * (1 + len(placements) // 4), rot, mirror))
    files, tree = _place(tmp_path, lib, "Q_PMOS_SGD", "Transistor_FET:Q_PMOS_SGD", placements)
    for ref, *_ in placements:
        for p in ed.list_pins_for_symbol(tree, ref):
            ed.add_label(tree, f"{ref}_{p['number']}", *p["position_mm"])
    sch_io.write_file(files["sch"], tree)

    xml_path = tmp_path / "n.xml"
    r = subprocess.run([str(cli), "sch", "export", "netlist", "--format", "kicadxml",
                        "-o", str(xml_path), str(files["sch"])],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    nets = {net.get("name").lstrip("/"): {(nd.get("ref"), nd.get("pin"))
                                          for nd in net.findall("node")}
            for net in ET.parse(xml_path).getroot().find("nets")}
    bad = [(ref, rot, m, pin) for ref, _x, _y, rot, m in placements for pin in "123"
           if nets.get(f"{ref}_{pin}") != {(ref, pin)}]
    assert not bad, bad

    # and our own net builder agrees
    ours = netlist.build_sheet_nets(sch_io.parse_file(files["sch"]), files["sch"]).nets
    for ref, *_ in placements:
        for pin in "123":
            assert {(p["ref"], p["pin"]) for p in ours[f"{ref}_{pin}"]["pins"]} == {(ref, pin)}
