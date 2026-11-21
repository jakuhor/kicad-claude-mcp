"""OP-18: batch editing, pin-addressed connection, drag, field autoplace,
page setup, readability check, label justification and net naming."""

from __future__ import annotations

import copy
import glob
import subprocess
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_geometry as g
from kicad_claude.adapters import sch_io
from kicad_claude.adapters import sch_layout as layout
from kicad_claude.adapters import sch_netlist as netlist
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools.library import index as lib_tools
from kicad_claude.tools.sch import register as register_sch
from kicad_claude.utils.kicad_paths import find_kicad_cli, find_symbol_lib_dirs

DEMOS = Path("C:/Program Files/KiCad/10.0/share/kicad/demos")
WANTED = {"Device": ["R", "C", "LED"], "power": ["GND", "+5V", "VCC", "PWR_FLAG"],
          "Transistor_FET": ["2N7002"]}


def _loads(text: str) -> list:
    return sch_io._RawNumberParser(text).parse()[0]


def _kicad_lib(name: str) -> Path | None:
    for d in find_symbol_lib_dirs():
        p = Path(d) / f"{name}.kicad_sym"
        if p.is_file():
            return p
    return None


@pytest.fixture(scope="module")
def stock_index(tmp_path_factory):
    """An index of a few stock KiCAD symbols, copied into small libraries."""
    out = tmp_path_factory.mktemp("libs")
    for lib, names in WANTED.items():
        src = _kicad_lib(lib)
        if src is None:
            pytest.skip("KiCAD symbol libraries not installed")
        defs = [sch_io.dumps(ed.fetch_symbol_def(src, n)) for n in names]
        (out / f"{lib}.kicad_sym").write_text(
            '(kicad_symbol_lib (version 20251024) (generator "test")\n'
            + "\n".join(defs) + ")\n", encoding="utf-8")
    return kicad_libs.build_index(symbol_dirs=[out], footprint_dirs=[])


@pytest.fixture
def mcp(tmp_path, monkeypatch, stock_index):
    state.clear_active()
    write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    monkeypatch.setattr(lib_tools, "load_cache", lambda: stock_index)
    monkeypatch.setattr(lib_tools, "_index", None)
    server = FastMCP("t")
    register_sch(server)
    yield server
    state.clear_active()


def _call(server, name, /, **kw):
    return server._tool_manager.get_tool(name).fn(**kw)


def _tree():
    return sch_io.parse_file(state.get_active().sch_path)


def _nets(server):
    return {n["name"]: sorted(n["pins"]) for n in
            _call(server, "list_sch_nets", compact=True)["nets"]}


BASE = [
    {"op": "symbol", "lib_id": "Device:R", "ref": "R1", "value": "10k",
     "x_mm": 50.8, "y_mm": 50.8},
    {"op": "symbol", "lib_id": "Device:R", "ref": "R2", "value": "4k7",
     "x_mm": 76.2, "y_mm": 63.5, "rotation": 90},
    {"op": "symbol", "lib_id": "Device:C", "ref": "C1", "value": "100n",
     "x_mm": 63.5, "y_mm": 50.8},
    {"op": "symbol", "lib_id": "Transistor_FET:2N7002", "ref": "Q1", "value": "2N7002",
     "x_mm": 101.6, "y_mm": 63.5},
    {"op": "connect", "pins": ["R1.1", "C1.1"], "net": "+5V"},
    {"op": "connect", "pins": ["C1.2", "Q1.2"], "net": "GND"},
    {"op": "connect", "pin": "Q1.3", "net": "OUT"},
    {"op": "wire", "from": "R1.2", "to": "R2.1"},
    {"op": "wire", "from": "R2.2", "to": "Q1.1"},
    {"op": "pwr_flag", "pin": "C1.1"},
    {"op": "pwr_flag", "pin": "C1.2"},
]


# ===== Labels ============================================================== #


@pytest.mark.parametrize("orientation,justify", [
    ("right", "left"), ("up", "left"), ("left", "right"), ("down", "right")])
def test_label_justify_matches_kicad(orientation, justify):
    """KiCAD 10 demos: labels at 0/90 are `left bottom`, at 180/270 `right bottom`."""
    tree = _loads("(kicad_sch (version 20260306) (uuid \"u\"))")
    node = ed.add_label(tree, "N", 10, 10, orientation)
    j = sch_io.find_child(sch_io.find_child(node, "effects"), "justify")
    assert [str(x) for x in j[1:]] == [justify, "bottom"]
    hl = ed.add_hierarchical_label(tree, net_name="H", x_mm=10, y_mm=10,
                                   orientation=orientation)
    j = sch_io.find_child(sch_io.find_child(hl, "effects"), "justify")
    assert [str(x) for x in j[1:]] == [justify]


def test_global_label_has_kicad_form():
    tree = _loads("(kicad_sch (version 20260306) (uuid \"u\"))")
    node = ed.add_global_label(tree, "SDA", 20, 30, "left", "output")
    assert str(sch_io.find_child(node, "shape")[1]) == "output"
    assert sch_io.find_child(node, "at")[1:] == [20.0, 30.0, 180]
    prop = sch_io.find_child(node, "property")
    assert prop[1] == "Intersheetrefs" and prop[2] == "${INTERSHEET_REFS}"


def test_global_label_merges_nets_across_sheets(mcp):
    _call(mcp, "edit_schematic", ops=[
        {"op": "symbol", "lib_id": "Device:R", "ref": "R1", "value": "1k",
         "x_mm": 50.8, "y_mm": 50.8},
        {"op": "connect", "pin": "R1.1", "net": "SIG", "style": "global"},
    ])
    tree = _tree()
    assert sch_io.find_child(tree, "global_label") is not None


# ===== Net naming and dangling ============================================= #


def test_unconnected_pins_with_one_name_stay_separate_nets(mcp):
    """kicad-cli: `unconnected-(U5-NC-Pad4)`, `unconnected-(U5-NC-Pad7)` — one
    net each, never one net of seven pins."""
    members_a = [{"ref": "U5", "pin": "4", "pin_name": "NC", "power": False}]
    members_b = [{"ref": "U5", "pin": "7", "pin_name": "NC", "power": False}]
    a = netlist._generated_name(members_a, connected=False)
    b = netlist._generated_name(members_b, connected=False)
    assert a == "unconnected-(U5-NC-Pad4)" and b == "unconnected-(U5-NC-Pad7)"
    assert netlist._generated_name(
        [{"ref": "Q3", "pin": "3", "pin_name": "D", "power": False}], True) == "Net-(Q3-D)"
    assert netlist._generated_name(
        [{"ref": "F1", "pin": "1", "pin_name": "", "power": False}], True) == "Net-(F1-Pad1)"
    assert netlist._generated_name(
        [{"ref": "U1", "pin": "1", "pin_name": "GND", "power": False, "_dup_name": True}],
        True) == "Net-(U1-GND-Pad1)"


def test_find_dangling_skips_no_connect_type_pins(mcp):
    tree = _tree()
    lib = _loads(
        '(symbol "T:NC2" (symbol "NC2_1_1"'
        ' (pin no_connect line (at -3.81 0 0) (length 2.54) (hide yes) (name "NC") (number "1"))'
        ' (pin passive line (at 3.81 0 180) (length 2.54) (name "A") (number "2"))))')
    ed.add_symbol(tree, qualified_lib_id="T:NC2", reference="U1", value="x", x_mm=50.8,
                  y_mm=50.8, rotation=0, sym_def_node=lib, project_name="p")
    dangling = netlist.build_sheet_nets(tree).dangling
    assert [d["pin"] for d in dangling if d["kind"] == "pin"] == ["2"]


# ===== edit_schematic ====================================================== #


def test_batch_builds_a_circuit_in_one_write(mcp):
    res = _call(mcp, "edit_schematic", ops=BASE, check=True)
    assert res["applied"] == len(BASE)
    assert res["backup"]
    nets = _nets(mcp)
    assert nets["+5V"] == ["C1.1", "R1.1"]
    assert nets["GND"] == ["C1.2", "Q1.2"]
    assert nets["OUT"] == ["Q1.3"]
    assert ["R2.2", "Q1.1"] in [sorted(v, reverse=True) for v in nets.values()] or \
        any(sorted(v) == ["Q1.1", "R2.2"] for v in nets.values())
    assert any(sorted(v) == ["R1.2", "R2.1"] for v in nets.values())
    assert "readability" in res
    assert res["by_op"] == {"symbol": 4, "connect": 3, "wire": 2, "pwr_flag": 2}
    # Compact: only what the batch made up itself (port and flag refs).
    assert all(set(r) - {"i"} <= {"ref", "refs"} for r in res["results"])
    assert sum(len(r.get("refs", [])) for r in res["results"]) == 4   # +5V x2, GND x2
    verbose = _call(mcp, "edit_schematic", ops=[], verbose=True)
    assert verbose["applied"] == 0 and "results" not in verbose


def test_batch_is_all_or_nothing(mcp):
    before = state.get_active().sch_path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match=r"op 1 \(connect\).*nothing written"):
        _call(mcp, "edit_schematic", ops=[BASE[0], {"op": "connect", "pin": "R9.1",
                                                     "net": "X"}])
    assert state.get_active().sch_path.read_text(encoding="utf-8") == before


def test_batch_dry_run_writes_nothing(mcp):
    before = state.get_active().sch_path.read_text(encoding="utf-8")
    res = _call(mcp, "edit_schematic", ops=BASE[:1], dry_run=True)
    assert res["dry_run"] is True
    assert state.get_active().sch_path.read_text(encoding="utf-8") == before


def test_connect_points_label_and_port_away_from_the_body(mcp):
    _call(mcp, "edit_schematic", ops=BASE)
    tree = _tree()
    q1 = ed.find_symbol_by_reference(tree, "Q1")
    drain = next(p for p in g.pins(tree, q1) if p["number"] == "3")
    label = next(n for n in tree[1:] if sch_io.is_call(n, "label") and n[1] == "OUT")
    at = sch_io.find_child(label, "at")
    # Stub of 2.54 mm straight out of the pin, label reading away from it.
    assert (at[1], at[2]) == (drain["point"][0], round(drain["point"][1] - 2.54, 4))
    assert at[3] == 90
    gnd = [s for s in ed.iter_instance_symbols(tree)
           if ed.get_symbol_property(s, "Value") == "GND"]
    # C1.2 points down: GND unrotated. Q1.2 (source, bottom) too.
    assert all(sch_io.find_child(s, "at")[3] == 0 for s in gnd)


def test_wire_from_to_routes_an_l(mcp):
    _call(mcp, "edit_schematic", ops=BASE[:4] + [{"op": "wire", "from": "R1.2", "to": "R2.1"}])
    tree = _tree()
    wires = sch_io.find_children(tree, "wire")
    assert len(wires) == 2
    pts = [tuple(float(v) for v in xy[1:3]) for w in wires
           for xy in sch_io.find_children(sch_io.find_child(w, "pts"), "xy")]
    # Every segment is horizontal or vertical.
    for a, b in zip(pts[::2], pts[1::2]):
        assert a[0] == b[0] or a[1] == b[1]


# ===== move with drag ====================================================== #


def test_drag_carries_stubs_labels_and_ports(mcp):
    _call(mcp, "edit_schematic", ops=BASE)
    res = _call(mcp, "move_item", reference="Q1", x_mm=101.6, y_mm=76.2, drag=True)
    assert res["dragged"] >= 3
    nets = _nets(mcp)
    assert nets["OUT"] == ["Q1.3"]
    assert nets["GND"] == ["C1.2", "Q1.2"]
    assert any(sorted(v) == ["Q1.1", "R2.2"] for v in nets.values())
    tree = _tree()
    label = next(n for n in tree[1:] if sch_io.is_call(n, "label") and n[1] == "OUT")
    q1 = ed.find_symbol_by_reference(tree, "Q1")
    drain = next(p for p in g.pins(tree, q1) if p["number"] == "3")
    assert sch_io.find_child(label, "at")[2] == round(drain["point"][1] - 2.54, 4)


def test_move_without_drag_leaves_connections(mcp):
    _call(mcp, "edit_schematic", ops=BASE)
    res = _call(mcp, "move_item", reference="Q1", x_mm=101.6, y_mm=76.2)
    assert "dragged" not in res
    assert "OUT" not in _nets(mcp) or _nets(mcp)["OUT"] != ["Q1.3"]


# ===== autoplace =========================================================== #


def test_rotated_resistor_gets_fields_above(mcp):
    _call(mcp, "add_symbol", lib_id="Device:R", reference="R1", value="10k",
          x_mm=50.8, y_mm=50.8, rotation=90)
    s = ed.find_symbol_by_reference(_tree(), "R1")
    at = {p[1]: sch_io.find_child(p, "at") for p in sch_io.find_children(s, "property")}
    assert at["Reference"][1] == at["Value"][1] == 50.8
    assert at["Reference"][2] < at["Value"][2] < 50.8 - 1.0
    assert sch_io.find_child(s, "fields_autoplaced") is not None


@pytest.mark.skipif(not DEMOS.is_dir(), reason="KiCAD demos not installed")
def test_autoplace_matches_kicad_demos_for_unrotated_passives():
    """R / R_Small / C at 0 and 180 degrees land where KiCAD put them.

    Only placements on the 1.27 mm grid count: older demos carry fields from
    KiCAD versions that autoplaced off grid (offsets like 1.778 / 0.594)."""
    total = exact = 0
    for f in glob.glob(str(DEMOS / "**" / "*.kicad_sch"), recursive=True):
        tree = sch_io.parse_file(f)
        for n in list(ed.iter_instance_symbols(tree)):
            lib = sch_io.find_child(n, "lib_id")[1]
            rot = float(sch_io.find_child(n, "at")[3])
            if lib not in ("Device:R", "Device:R_Small", "Device:C") or rot not in (0, 180):
                continue
            if sch_io.find_child(n, "fields_autoplaced") is None:
                continue
            want = {p[1]: tuple(sch_io.find_child(p, "at")[1:3])
                    for p in sch_io.find_children(n, "property")
                    if p[1] in ("Reference", "Value") and not ed._field_hidden(p)}
            sx, sy = (float(v) for v in sch_io.find_child(n, "at")[1:3])
            if any(abs(((v - o) / 1.27) - round((v - o) / 1.27)) > 0.01
                   for x, y in want.values() for v, o in ((x, sx), (y, sy))):
                continue
            m = copy.deepcopy(n)
            tree.append(m)
            layout.autoplace_fields(tree, m)
            tree.pop()
            got = {p[1]: tuple(sch_io.find_child(p, "at")[1:3])
                   for p in sch_io.find_children(m, "property") if p[1] in want}
            total += 1
            exact += all(abs(got[k][0] - want[k][0]) < 0.02 and abs(got[k][1] - want[k][1]) < 0.02
                         for k in want)
    assert total >= 25
    assert exact == total


# ===== page ================================================================ #


def test_set_page_paper_and_title_block(mcp):
    res = _call(mcp, "set_page", paper="A3",
                title_block={"title": "LED control", "rev": "2", "comment1": "first"})
    assert res["page_mm"] == [420.0, 297.0]
    assert res["title_block"] == {"title": "LED control", "rev": "2", "comment1": "first"}
    res = _call(mcp, "set_page", title_block={"rev": "", "date": "2026-11-14"})
    assert res["title_block"] == {"title": "LED control", "date": "2026-11-14",
                                  "comment1": "first"}
    tree = _tree()
    assert sch_io.find_child(tree, "paper")[1] == "A3"
    res = _call(mcp, "set_page", paper="A4", portrait=True)
    assert res["page_mm"] == [210.0, 297.0]


# ===== readability ========================================================= #


def test_readability_flags_overlaps_and_title_block(mcp):
    _call(mcp, "edit_schematic", ops=[
        {"op": "symbol", "lib_id": "Device:R", "ref": "R1", "value": "10k",
         "x_mm": 50.8, "y_mm": 50.8},
        {"op": "label", "net": "AAAA_LONG_NAME", "x_mm": 40.64, "y_mm": 50.8},
        {"op": "label", "net": "BBBB_LONG_NAME", "x_mm": 43.18, "y_mm": 50.8},
        {"op": "symbol", "lib_id": "Device:R", "ref": "R2", "value": "1k",
         "x_mm": 270.51, "y_mm": 190.5},
    ])
    res = _call(mcp, "check_schematic_readability")
    rules = res["counts"]
    assert rules.get("SR-OVERLAP", 0) >= 1
    assert rules.get("SR-TITLE-BLOCK", 0) >= 1
    # The short value inside R1's body is deliberate.
    assert not any("R1.Value" in f["message"] for f in res["findings"]
                   if f["rule"] == "SR-TEXT-ON-BODY")


def test_readability_flags_label_running_into_a_body(mcp):
    _call(mcp, "edit_schematic", ops=[
        {"op": "symbol", "lib_id": "Device:R", "ref": "R1", "value": "10k",
         "x_mm": 50.8, "y_mm": 50.8},
        {"op": "label", "net": "INTO", "pin": "R1.1", "orientation": "down"},
    ])
    res = _call(mcp, "check_schematic_readability")
    assert res["counts"].get("SR-LABEL-INTO-BODY") == 1


def test_power_warning_comes_once_per_net(mcp):
    r = _call(mcp, "add_power_symbol", net="VIN", x_mm=50.8, y_mm=50.8)
    assert any("PWR_FLAG" in w for w in r["warnings"])
    r = _call(mcp, "add_power_symbol", net="VIN", x_mm=63.5, y_mm=50.8)
    assert "warnings" not in r


# ===== kicad-cli =========================================================== #


@pytest.mark.slow
def test_batch_built_sheet_passes_erc_and_names_match_kicad(mcp, tmp_path):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not found")
    ops = BASE + [{"op": "connect", "pin": "Q1.3", "net": "OUT2", "style": "global"}]
    ops[6] = {"op": "no_connect", "pin": "Q1.3"}
    _call(mcp, "edit_schematic", ops=ops[:-1])
    sch = state.get_active().sch_path
    out = tmp_path / "erc.json"
    subprocess.run([str(cli), "sch", "erc", "--format", "json", "--severity-error",
                    "-o", str(out), str(sch)], check=True, capture_output=True)
    import json
    report = json.loads(out.read_text(encoding="utf-8"))
    errors = [v for s in report["sheets"] for v in s["violations"]]
    assert errors == []
    net = tmp_path / "n.net"
    subprocess.run([str(cli), "sch", "export", "netlist", "-o", str(net), str(sch)],
                   check=True, capture_output=True)
    text = net.read_text(encoding="utf-8")
    for name in _nets(mcp):
        assert f'(name "{name}")' in text, name


# ===== OP-23: edits that kept connectivity broken ========================== #

_TWO_PARTS = [
    {"op": "symbol", "lib_id": "Device:R", "ref": "R1", "value": "1k", "x_mm": 50.8, "y_mm": 50.8},
    {"op": "symbol", "lib_id": "Device:R", "ref": "R2", "value": "1k", "x_mm": 76.2, "y_mm": 50.8},
]


def _r_tops(server):
    tree = _tree()
    return [min((p["point"] for p in g.pins(tree, ed.find_symbol_by_reference(tree, r))),
                key=lambda pt: pt[1]) for r in ("R1", "R2")]


def test_power_port_and_flag_mid_wire_get_a_junction(mcp):
    _call(mcp, "edit_schematic", ops=_TWO_PARTS)
    a, b = _r_tops(mcp)
    y = round(a[1] - 5.08, 4)
    mid = round((a[0] + b[0]) / 2, 4)
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "wire", "points": [list(a), [a[0], y], [b[0], y], list(b)]},
        {"op": "power", "net": "VIN", "x_mm": mid, "y_mm": y},
        {"op": "pwr_flag", "x_mm": mid - 5.08, "y_mm": y},
    ])
    tree = _tree()
    junctions = {(float(j[1][1]), float(j[1][2])) for j in sch_io.find_children(tree, "junction")}
    assert (mid, y) in junctions and (mid - 5.08, y) in junctions
    assert "warnings" not in res          # the flag in the same batch settles VIN
    assert _nets(mcp)["VIN"] == ["R1.1", "R2.1"] or sorted(_nets(mcp)["VIN"]) == ["R1.1", "R2.1"]


def test_remove_follows_a_bent_stub_to_the_net(mcp):
    """Field test 2 #3: a flag on a side stub, dragged into an L, then removed."""
    _call(mcp, "edit_schematic", ops=_TWO_PARTS + [{"op": "connect", "pin": "R1.1", "net": "GND"}])
    port = next(s for s in ed.iter_instance_symbols(_tree())
                if ed.get_symbol_property(s, "Value") == "GND")
    px, py = (float(v) for v in sch_io.find_child(port, "at")[1:3])
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "wire", "points": [[px, py], [px - 5.08, py]]},
        {"op": "pwr_flag", "x_mm": px - 5.08, "y_mm": py, "rotation": 0}])
    ref = next(r["ref"] for r in res["results"] if r["i"] == 1)
    _call(mcp, "edit_schematic", ops=[{"op": "move", "ref": ref, "x_mm": px - 5.08,
                                       "y_mm": py + 2.54}])
    # The drag bends the stub without laying the bend on the GND stub.
    tree = _tree()
    assert not any(layout._overlaps_wire(tree, w, [tuple(
        (float(xy[1]), float(xy[2])) for xy in sch_io.find_children(sch_io.find_child(w, "pts"), "xy"))])
        for w in sch_io.find_children(tree, "wire"))
    wires_before = len(sch_io.find_children(_tree(), "wire"))
    res = _call(mcp, "edit_schematic", ops=[{"op": "remove", "ref": ref}])
    removed = res["results"][0]["wires"]
    assert removed >= 2
    assert len(sch_io.find_children(_tree(), "wire")) == wires_before - removed
    assert [d for d in _call(mcp, "find_dangling")["items"] if d["kind"] == "wire_end"] == []
    assert _nets(mcp)["GND"] == ["R1.1"]


def test_find_dangling_reports_a_lone_end_of_a_connected_wire(mcp):
    _call(mcp, "edit_schematic", ops=_TWO_PARTS)
    a, _ = _r_tops(mcp)
    _call(mcp, "edit_schematic", ops=[{"op": "wire", "points": [list(a), [a[0], a[1] - 5.08]]}])
    ends = [tuple(d["position_mm"]) for d in _call(mcp, "find_dangling")["items"]
            if d["kind"] == "wire_end"]
    assert ends == [(a[0], round(a[1] - 5.08, 4))]


# ===== OP-24: layout quality ================================================ #


def test_mirror_on_place_and_move_mirrors_the_pins(mcp):
    _call(mcp, "edit_schematic", ops=[
        {"op": "symbol", "lib_id": "Transistor_FET:2N7002", "ref": "Q1", "value": "x",
         "x_mm": 50.8, "y_mm": 50.8},
        {"op": "symbol", "lib_id": "Transistor_FET:2N7002", "ref": "Q2", "value": "x",
         "x_mm": 101.6, "y_mm": 50.8, "mirror": "x"}])
    tree = _tree()
    q1 = {p["number"]: p["position_mm"] for p in ed.list_pins_for_symbol(tree, "Q1")}
    q2 = {p["number"]: p["position_mm"] for p in ed.list_pins_for_symbol(tree, "Q2")}
    # Mirror about X swaps drain (top) and source (bottom); the gate stays.
    assert q2["3"][1] - 50.8 == pytest.approx(-(q1["3"][1] - 50.8))
    assert q2["1"][1] == q1["1"][1]
    # list_pins and the netlist geometry agree on a mirrored part.
    geo = {p["number"]: list(p["point"]) for p in g.pins(tree, ed.find_symbol_by_reference(tree, "Q2"))}
    assert geo == {k: [pytest.approx(v[0]), pytest.approx(v[1])] for k, v in q2.items()}
    _call(mcp, "edit_schematic", ops=[{"op": "move", "ref": "Q2", "x_mm": 101.6, "y_mm": 50.8,
                                       "mirror": ""}])
    assert sch_io.find_child(ed.find_symbol_by_reference(_tree(), "Q2"), "mirror") is None


def test_power_port_on_a_side_pin_stands_upright(mcp):
    _call(mcp, "edit_schematic", ops=[
        {"op": "symbol", "lib_id": "Device:R", "ref": "R1", "value": "1k", "x_mm": 50.8,
         "y_mm": 50.8, "rotation": 90},
        {"op": "connect", "pin": "R1.1", "net": "+5V"}])
    port = next(s for s in ed.iter_instance_symbols(_tree())
                if ed.get_symbol_property(s, "Value") == "+5V")
    assert float(sch_io.find_child(port, "at")[3]) == 0
    assert _nets(mcp)["+5V"] == ["R1.1"]


def test_text_width_uses_the_measured_stroke_font():
    assert g.text_extent("iiii", 1.27)[0] < g.text_extent("MMMM", 1.27)[0] / 2
    # Measured with kicad-cli 10.0.8: "SN74AHCT1G125DBVR" spans 20.1 mm at 1.27.
    assert g.text_extent("SN74AHCT1G125DBVR", 1.27)[0] == pytest.approx(20.1, abs=0.6)


def test_forced_autoplace_keeps_fields_off_wires(mcp):
    _call(mcp, "edit_schematic", ops=[
        {"op": "symbol", "lib_id": "Transistor_FET:2N7002", "ref": "Q1", "value": "2N7002",
         "x_mm": 50.8, "y_mm": 50.8}])
    tree = _tree()
    q = ed.find_symbol_by_reference(tree, "Q1")
    pins = {p["number"]: p for p in g.pins(tree, q)}
    ops = []
    for num in pins:   # a long wire out of every pin
        (px, py), o = pins[num]["point"], pins[num]["out"]
        ops.append({"op": "wire", "points": [[px, py], [px + o[0] * 12.7, py + o[1] * 12.7]]})
    ops.append({"op": "autoplace", "refs": ["Q1"], "force": True})
    _call(mcp, "edit_schematic", ops=ops)
    tree = _tree()
    q = ed.find_symbol_by_reference(tree, "Q1")
    boxes = [g.field_box(q, f) for f in g.visible_fields(q)]
    wires = layout._items(tree)["wires"]
    assert not any(g.segment_hits_box(u, v, g.shrink(bx, 0.1)) for bx in boxes for u, v in wires)


@pytest.mark.slow
def test_side_ports_and_mid_stub_flags_pass_erc(mcp, tmp_path):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not found")
    _call(mcp, "edit_schematic", ops=[
        {"op": "symbol", "lib_id": "Device:R", "ref": "R1", "value": "1k", "x_mm": 50.8,
         "y_mm": 50.8, "rotation": 90},
        {"op": "connect", "pin": "R1.1", "net": "+5V"},
        {"op": "connect", "pin": "R1.2", "net": "GND"},
        {"op": "pwr_flag", "pin": "R1.1"},
        {"op": "pwr_flag", "pin": "R1.2"}])
    out = tmp_path / "erc.json"
    subprocess.run([str(cli), "sch", "erc", "--format", "json", "-o", str(out),
                    str(state.get_active().sch_path)], check=True, capture_output=True)
    import json
    report = json.loads(out.read_text(encoding="utf-8"))
    assert [v["type"] for s in report["sheets"] for v in s["violations"]] == []


def test_list_pins_groups_stacked_pins(mcp):
    _call(mcp, "edit_schematic", ops=_TWO_PARTS)
    tree = _tree()
    r = ed.find_symbol_by_reference(tree, "R1")
    # Stack pin 2 onto pin 1 in the cached library copy: same name, same point.
    lib = ed.find_lib_symbol_def(tree, "Device:R")
    pins = [p for p, _u in ed._iter_pins(lib)]
    for p in pins:
        sch_io.find_child(p, "at")[1:4] = list(sch_io.find_child(pins[0], "at")[1:4])
        sch_io.find_child(p, "name")[1] = "X"
    sch_io.write_file(state.get_active().sch_path, tree)
    grouped = _call(mcp, "list_pins", reference="R1")
    assert len(grouped) == 1 and grouped[0]["numbers"] == ["1", "2"]
    assert len(_call(mcp, "list_pins", reference="R1", group_stacked=False)) == 2
    assert r is not None


# ===== Field test 3 (OP-37 / OP-38 / OP-39) ================================ #


R1_ONLY = [{"op": "symbol", "lib_id": "Device:R", "ref": "R1", "value": "10k",
            "x_mm": 50.8, "y_mm": 50.8}]


def _wire_count() -> int:
    return len(sch_io.find_children(_tree(), "wire"))


def test_removing_a_port_on_a_bottom_pin_takes_its_straight_stub(mcp):
    _call(mcp, "edit_schematic", ops=R1_ONLY + [{"op": "connect", "pin": "R1.2", "net": "GND"}])
    assert _wire_count() == 1
    port = next(ed.get_symbol_property(s, "Reference") for s in ed.iter_instance_symbols(_tree())
                if ed.get_symbol_property(s, "Value") == "GND")
    res = _call(mcp, "edit_schematic", ops=[{"op": "remove", "ref": port}])
    assert res["results"][0]["wires"] == 1
    assert _wire_count() == 0


def test_removing_a_part_keeps_a_wire_to_another_pin(mcp):
    _call(mcp, "edit_schematic", ops=R1_ONLY + [
        {"op": "symbol", "lib_id": "Device:R", "ref": "R2", "value": "1k",
         "x_mm": 50.8, "y_mm": 76.2},
        {"op": "wire", "from": "R1.2", "to": "R2.1"}])
    _call(mcp, "edit_schematic", ops=[{"op": "remove", "ref": "R1"}])
    assert _wire_count() == 1


TWO_R = R1_ONLY + [{"op": "symbol", "lib_id": "Device:R", "ref": "R2", "value": "1k",
                    "x_mm": 76.2, "y_mm": 50.8}]


def _flags() -> int:
    return sum(1 for s in ed.iter_instance_symbols(_tree())
               if (ed.get_symbol_property(s, "Reference") or "").startswith("#FLG"))


def test_connect_flag_places_one_flag_per_net(mcp):
    _call(mcp, "edit_schematic", ops=TWO_R + [
        {"op": "connect", "pins": ["R1.2", "R2.2"], "net": "GND", "flag": True},
        {"op": "connect", "pin": "R1.1", "net": "GND", "flag": True}])
    assert _flags() == 1
    _call(mcp, "edit_schematic", ops=[{"op": "connect", "pin": "R2.1", "net": "GND",
                                       "flag": True}])
    assert _flags() == 1


def test_stock_power_port_without_driver_gets_a_reminder(mcp):
    res = _call(mcp, "edit_schematic", ops=TWO_R + [
        {"op": "connect", "pins": ["R1.1", "R2.1"], "net": "+5V"}])
    assert any("'+5V'" in w and "driver" in w for w in res["warnings"])
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "connect", "pin": "R1.2", "net": "+5V", "flag": True}])
    assert "warnings" not in res, res["warnings"]


def test_move_that_merges_two_nets_is_refused(mcp):
    _call(mcp, "edit_schematic", ops=TWO_R + [
        {"op": "connect", "pin": "R1.1", "net": "A"},
        {"op": "connect", "pin": "R1.2", "net": "A2"},
        {"op": "connect", "pin": "R2.1", "net": "B"},
        {"op": "connect", "pin": "R2.2", "net": "B2"}])
    move = {"op": "move", "ref": "R2", "x_mm": 50.8, "y_mm": 48.26}
    with pytest.raises(ValueError, match="merges nets"):
        _call(mcp, "edit_schematic", ops=[move])
    res = _call(mcp, "edit_schematic", ops=[{**move, "allow_merge": True}])
    assert ["A", "B"] in res["results"][0]["nets_merged"]


def test_move_of_a_lone_pin_onto_another_net_is_refused(mcp):
    """OP-50: a pin with nothing on it that lands on another net's label is
    a merge too, not only two named nets."""
    _call(mcp, "edit_schematic", ops=TWO_R + [
        {"op": "connect", "pin": "R1.1", "net": "A"},
        {"op": "connect", "pin": "R1.2", "net": "A2"}])
    a = next(lb for lb in sch_io.find_children(_tree(), "label") if lb[1] == "A")
    at = sch_io.find_child(a, "at")
    # R2's pin 1 sits 3.81 mm above its origin (Device:R, rotation 0).
    move = {"op": "move", "ref": "R2", "x_mm": float(at[1]), "y_mm": float(at[2]) + 3.81}
    # A free spot is no merge.
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "move", "ref": "R2", "x_mm": 127, "y_mm": 101.6}])
    assert "nets_merged" not in (res.get("results") or [{}])[0]
    with pytest.raises(ValueError, match="merges nets.*R2.1"):
        _call(mcp, "edit_schematic", ops=[move])
    res = _call(mcp, "edit_schematic", ops=[{**move, "allow_merge": True}])
    assert any("R2.1" in m for m in res["results"][0]["nets_merged"])


def test_check_reports_dangling_wire_ends(mcp):
    """OP-51: a wire drawn past its label leaves a free end ERC flags;
    `check=True` names it."""
    res = _call(mcp, "edit_schematic", check=True, ops=R1_ONLY + [
        {"op": "wire", "x1_mm": 101.6, "y1_mm": 101.6, "x2_mm": 114.3, "y2_mm": 101.6},
        {"op": "label", "net": "X", "x_mm": 101.6, "y_mm": 101.6}])
    assert res["dangling"]["wire_ends"] == [[114.3, 101.6]]
    assert res["dangling"]["pins"] == 2


def test_wire_over_an_existing_wire_warns(mcp):
    """OP-52: a new wire collinear over an old one is flagged."""
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "wire", "x1_mm": 101.6, "y1_mm": 101.6, "x2_mm": 127, "y2_mm": 101.6},
        {"op": "wire", "x1_mm": 114.3, "y1_mm": 101.6, "x2_mm": 139.7, "y2_mm": 101.6},
        {"op": "wire", "x1_mm": 139.7, "y1_mm": 101.6, "x2_mm": 139.7, "y2_mm": 114.3}])
    assert sum("collinear" in w for w in res["warnings"]) == 1


def test_readability_sees_ports_flags_and_the_border_band(mcp):
    _call(mcp, "edit_schematic", ops=R1_ONLY + [
        {"op": "power", "net": "+5V", "x_mm": 101.6, "y_mm": 50.8, "rotation": 0},
        {"op": "power", "net": "+5V", "x_mm": 101.6, "y_mm": 50.8, "rotation": 0},
        {"op": "label", "net": "EDGE", "x_mm": 11.43, "y_mm": 100.33}])
    res = _call(mcp, "check_schematic_readability")
    msgs = [f["message"] for f in res["findings"]]
    assert any("stacked" in m for m in msgs)
    assert any("EDGE" in m and "border" in m for m in msgs)


def test_labels_list_and_bulk_remove_and_multi_ref_remove(mcp):
    _call(mcp, "edit_schematic", ops=TWO_R + [
        {"op": "connect", "pins": ["R1.1", "R2.1"], "net": "SIG", "join": False},
        {"op": "connect", "pins": ["R1.2", "R2.2"], "net": "GND"}])
    labels = _call(mcp, "list_texts", kind="labels", net="SIG")
    assert len(labels) == 2 and {lab["orientation"] for lab in labels} == {"up"}
    assert all(lab["uuid"] for lab in labels)
    x, y = labels[0]["position_mm"]
    _call(mcp, "edit_schematic", ops=[{"op": "remove", "label": "SIG", "x_mm": x, "y_mm": y}])
    assert len(_call(mcp, "list_texts", kind="labels")) == 1
    res = _call(mcp, "edit_schematic", ops=[{"op": "remove", "label": "SIG"},
                                            {"op": "remove", "pattern": "#PWR*"}])
    assert _call(mcp, "list_texts", kind="labels") == []
    assert res["results"] == [{"i": 0, "wires": 1}, {"i": 1, "wires": 2}]
    assert not any((ed.get_symbol_property(s, "Reference") or "").startswith("#PWR")
                   for s in ed.iter_instance_symbols(_tree()))
    _call(mcp, "edit_schematic", ops=[{"op": "remove", "refs": ["R1", "R2"]}])
    assert list(ed.iter_instance_symbols(_tree())) == []


def test_connect_wires_close_pins_under_one_label(mcp):
    ops = R1_ONLY + [{"op": "symbol", "lib_id": "Device:R", "ref": "R2", "value": "1k",
                      "x_mm": 58.42, "y_mm": 50.8},
                     {"op": "symbol", "lib_id": "Device:R", "ref": "R3", "value": "1k",
                      "x_mm": 101.6, "y_mm": 50.8},
                     {"op": "connect", "pins": ["R1.1", "R2.1", "R3.1"], "net": "SIG"}]
    _call(mcp, "edit_schematic", ops=ops)
    labels = _call(mcp, "list_texts", kind="labels", net="SIG")
    assert len(labels) == 2          # R1 + R2 share one; R3 is 50 mm away
    assert _nets(mcp)["SIG"] == ["R1.1", "R2.1", "R3.1"]
    assert _call(mcp, "check_schematic_readability")["counts"].get("SR-SHORT-LABEL-NET", 0) == 0


def test_unknown_lib_ids_are_refused_up_front_with_a_hint(mcp):
    with pytest.raises(ValueError, match=r"op 1: unknown lib_id 'Devices:R' \(did you mean "
                                         r"'Device:R'\?\).*nothing written"):
        _call(mcp, "edit_schematic", ops=R1_ONLY + [
            {"op": "symbol", "lib_id": "Devices:R", "ref": "R2", "value": "1k",
             "x_mm": 76.2, "y_mm": 50.8}])
    assert list(ed.iter_instance_symbols(_tree())) == []


def test_removing_a_label_takes_its_stub_unless_told_otherwise(mcp):
    _call(mcp, "edit_schematic", ops=TWO_R + [{"op": "connect", "pin": "R1.1", "net": "A"},
                                              {"op": "connect", "pin": "R2.1", "net": "B"}])
    assert _wire_count() == 2
    res = _call(mcp, "edit_schematic", ops=[{"op": "remove", "label": "A"}])
    assert res["results"][0]["wires"] == 1
    assert _wire_count() == 1
    _call(mcp, "edit_schematic", ops=[{"op": "remove", "label": "B", "wires": False}])
    assert _wire_count() == 1


def test_led_arrows_do_not_count_as_body_for_wires(mcp):
    led = [{"op": "symbol", "lib_id": "Device:LED", "ref": "D1", "value": "red",
            "x_mm": 50.8, "y_mm": 50.8}]
    # K pin end at x 46.99: a wire going left runs under the arrows' box only
    _call(mcp, "edit_schematic", ops=led + [
        {"op": "wire", "x1_mm": 46.99, "y1_mm": 50.8, "x2_mm": 39.37, "y2_mm": 50.8}])
    res = _call(mcp, "check_schematic_readability")
    assert "SR-WIRE-THROUGH-BODY" not in res["counts"], res["findings"]
    # a wire straight through the diode triangle still counts
    _call(mcp, "edit_schematic", ops=[
        {"op": "wire", "x1_mm": 50.8, "y1_mm": 43.18, "x2_mm": 50.8, "y2_mm": 58.42}])
    res = _call(mcp, "check_schematic_readability")
    assert res["counts"].get("SR-WIRE-THROUGH-BODY") == 1


def test_label_only_ratio_and_short_label_distance(mcp):
    _call(mcp, "edit_schematic", ops=TWO_R + [
        {"op": "connect", "pins": ["R1.1", "R2.1"], "net": "SIG", "join": False},
        {"op": "wire", "from": "R1.2", "to": "R2.2"}])
    res = _call(mcp, "check_schematic_readability")
    assert res["label_only_nets"] == {"label_joined": 1, "signal_nets": 2, "ratio": 0.5}
    assert "SR-SHORT-LABEL-NET" not in res["counts"]  # 25.4 mm apart
    res = _call(mcp, "check_schematic_readability", short_label_mm=30)
    assert res["counts"].get("SR-SHORT-LABEL-NET") == 1


def test_mirrored_symbol_returns_pin_positions(mcp):
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "symbol", "lib_id": "Device:LED", "ref": "D1", "value": "red",
         "x_mm": 50.8, "y_mm": 50.8, "mirror": "y"}])
    pins = res["results"][0]["pins"]
    assert pins["1"][0] > 50.8 > pins["2"][0]  # mirror y: K moves to the right
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "move", "ref": "D1", "x_mm": 50.8, "y_mm": 50.8, "rotation": 180,
         "mirror": "x"}])
    assert res["results"][0]["pins"] == pins  # rot 180 + mirror x == mirror y


def test_connect_puts_one_label_on_the_wire_not_on_a_stub(mcp):
    ops = R1_ONLY + [{"op": "symbol", "lib_id": "Device:R", "ref": "R2", "value": "1k",
                      "x_mm": 58.42, "y_mm": 50.8},
                     {"op": "connect", "pins": ["R1.1", "R2.1"], "net": "SIG"}]
    res = _call(mcp, "edit_schematic", ops=ops, verbose=True)
    assert res["results"][-1]["labels"] == 1
    assert _wire_count() == 1  # the pin-to-pin wire, no stub
    (lab,) = _call(mcp, "list_texts", kind="labels", net="SIG")
    x, y = lab["position_mm"]
    assert abs(y - 46.99) < 1e-3 and 50.8 <= x <= 58.42
    assert _nets(mcp)["SIG"] == ["R1.1", "R2.1"]


def test_connect_name_false_wires_without_a_label(mcp):
    res = _call(mcp, "edit_schematic", ops=TWO_R + [
        {"op": "connect", "pins": ["R1.1", "R2.1"], "net": "SIG", "name": False}],
        verbose=True)
    assert res["results"][-1]["labels"] == 0
    assert _call(mcp, "list_texts", kind="labels") == []
    assert ["R1.1", "R2.1"] in _nets(mcp).values()


def test_name_net_labels_a_wired_net_on_its_wire(mcp):
    _call(mcp, "edit_schematic", ops=TWO_R + [{"op": "wire", "from": "R1.2", "to": "R2.2"}])
    res = _call(mcp, "edit_schematic", ops=[{"op": "name_net", "pin": "R1.2", "net": "MID"}])
    spot = res["results"][0]
    assert spot.get("collisions", 0) == 0 and spot["orientation"] == "right"
    assert _wire_count() == 1
    assert _nets(mcp)["MID"] == ["R1.2", "R2.2"]
    # a pin on no wire falls back to a stub and a label
    res = _call(mcp, "edit_schematic", ops=[{"op": "name_net", "pin": "R1.1", "net": "TOP"}])
    assert res["results"][0]["stub"] is True and _wire_count() == 2
    with pytest.raises(ValueError, match="no wire end"):
        _call(mcp, "edit_schematic", ops=[{"op": "name_net", "x_mm": 10, "y_mm": 10,
                                           "net": "X"}])


def test_autoplace_moves_fields_off_a_neighbour(mcp):
    # R2 sits where R1's fields go by default (right of its body)
    _call(mcp, "edit_schematic", ops=R1_ONLY + [
        {"op": "symbol", "lib_id": "Device:R", "ref": "R2", "value": "1k",
         "x_mm": 54.61, "y_mm": 50.8},
        {"op": "autoplace", "refs": ["R1"], "force": True}])
    r1 = ed.find_symbol_by_reference(_tree(), "R1")
    r2_body = g.body_box(_tree(), ed.find_symbol_by_reference(_tree(), "R2"))
    for f in g.visible_fields(r1):
        assert g.overlap_area(g.field_box(r1, f), r2_body) == 0
        assert float(sch_io.find_child(f, "at")[1]) < 50.8  # went to the left side


def test_field_justify_reads_as_asked_on_a_rotated_symbol(mcp):
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "symbol", "lib_id": "Device:R", "ref": "R1", "value": "10k",
         "x_mm": 50.8, "y_mm": 50.8, "rotation": 180},
        {"op": "field", "ref": "R1", "field": "Value", "x_mm": 60.96, "y_mm": 50.8,
         "angle": 0, "justify": "left"}])
    x1, _y1, x2, _y2 = res["results"][-1]["bbox_mm"]
    assert x1 == pytest.approx(60.96, abs=1e-3) and x2 > x1  # starts at the anchor
    r1 = ed.find_symbol_by_reference(_tree(), "R1")
    prop = next(f for f in g.visible_fields(r1) if f[1] == "Value")
    assert g.justify_of(prop)[0] == "right"  # stored pre-flipped for rotation 180


def test_move_region_shifts_a_block_and_keeps_its_nets(mcp):
    _call(mcp, "edit_schematic", ops=TWO_R + [
        {"op": "connect", "pin": "R1.1", "net": "IN"},
        {"op": "wire", "from": "R1.2", "to": "R2.2"},
        {"op": "text", "text": "block", "x_mm": 45.72, "y_mm": 58.42}])
    before = _nets(mcp)
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "move_region", "x1_mm": 38.1, "y1_mm": 38.1, "x2_mm": 63.5, "y2_mm": 63.5,
         "dx_mm": 0, "dy_mm": 10.16}])
    assert res["results"][0]["symbols"] == 1
    assert _nets(mcp) == before
    r1 = ed.find_symbol_by_reference(_tree(), "R1")
    assert g.Frame(r1).y == pytest.approx(60.96)
    (lab,) = _call(mcp, "list_texts", kind="labels", net="IN")
    assert lab["position_mm"][1] == pytest.approx(46.99 - 2.54 + 10.16)
    # dropping R1 onto R2 puts IN on OUT: refused
    _call(mcp, "edit_schematic", ops=[{"op": "connect", "pin": "R2.1", "net": "OUT"}])
    with pytest.raises(ValueError, match="merges nets"):
        _call(mcp, "edit_schematic", ops=[
            {"op": "move_region", "x1_mm": 38.1, "y1_mm": 38.1, "x2_mm": 63.5,
             "y2_mm": 73.66, "dx_mm": 25.4, "dy_mm": -10.16}])


def _ports(net: str) -> list:
    return [s for s in ed.iter_instance_symbols(_tree())
            if g.is_power(_tree(), s) and ed.get_symbol_property(s, "Value") == net]


def test_join_power_replaces_per_pin_ports_with_one(mcp):
    _call(mcp, "edit_schematic", ops=TWO_R + [
        {"op": "connect", "pins": ["R1.2", "R2.2"], "net": "GND"}])
    assert len(_ports("GND")) == 2
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "join_power", "pins": ["R1.2", "R2.2"], "net": "GND"}])
    assert res["results"][0]["ports_removed"] == 2
    assert len(_ports("GND")) == 1
    assert _nets(mcp)["GND"] == ["R1.2", "R2.2"]


def test_join_power_on_one_side_puts_the_port_atop_the_spine(mcp):
    _call(mcp, "edit_schematic", ops=[
        {"op": "symbol", "lib_id": "Device:R", "ref": "R1", "value": "1k",
         "x_mm": 50.8, "y_mm": 50.8, "rotation": 90},
        {"op": "symbol", "lib_id": "Device:R", "ref": "R2", "value": "1k",
         "x_mm": 50.8, "y_mm": 58.42, "rotation": 90},
        {"op": "join_power", "pins": ["R1.2", "R2.2"], "net": "+5V"}])
    (port,) = _ports("+5V")
    p1 = ed.get_pin_position(_tree(), "R1", "2")
    (pp,) = [p["point"] for p in g.pins(_tree(), port)]
    assert pp[1] == pytest.approx(p1[1])  # at the top end, level with R1.2
    assert _nets(mcp)["+5V"] == ["R1.2", "R2.2"]
    counts = _call(mcp, "check_schematic_readability")["counts"]
    assert "SR-OVERLAP" not in counts and "SR-PORT-INTO-BODY" not in counts


def test_place_chain_builds_a_wired_filter(mcp):
    res = _call(mcp, "edit_schematic", ops=[
        {"op": "place_chain", "x_mm": 50.8, "y_mm": 50.8,
         "parts": [{"lib_id": "Device:R", "ref": "R1", "value": "1k"},
                   {"lib_id": "Device:R", "ref": "R2", "value": "1k"}],
         "shunts": {"1": [{"lib_id": "Device:C", "ref": "C1", "value": "1n", "to": "GND"},
                          {"lib_id": "Device:C", "ref": "C2", "value": "1n", "to": "GND"}],
                    "2": [{"lib_id": "Device:C", "ref": "C3", "value": "1n", "to": "GND"}]},
         "nets": {"0": "IN", "1": "MID", "2": "OUT"}}])
    nets = _nets(mcp)
    assert nets["IN"] == ["R1.1"]
    assert nets["MID"] == ["C1.1", "C2.1", "R1.2", "R2.1"]
    assert nets["OUT"] == ["C3.1", "R2.2"]
    assert nets["GND"] == ["C1.2", "C2.2", "C3.2"]
    r1, r2 = (ed.find_symbol_by_reference(_tree(), r) for r in ("R1", "R2"))
    assert ed.get_pin_position(_tree(), "R1", "1")[1] == pytest.approx(50.8)
    assert g.Frame(r2).x > g.Frame(r1).x  # laid out left to right
    counts = _call(mcp, "check_schematic_readability")["counts"]
    for rule in ("SR-WIRE-THROUGH-BODY", "SR-PORT-INTO-BODY", "SR-OVERLAP", "SR-OFF-GRID"):
        assert rule not in counts, (rule, counts)
    assert set(res["results"][0]["nodes"]) == {"0", "1", "2"}


@pytest.mark.slow
def test_new_layout_ops_pass_erc_and_match_kicad_nets(mcp, tmp_path):
    """OP-50/52/54/55/57 output, proven by kicad-cli: ERC clean, same nets."""
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not found")
    _call(mcp, "edit_schematic", ops=[
        {"op": "place_chain", "x_mm": 50.8, "y_mm": 50.8,
         "parts": [{"lib_id": "Device:R", "ref": "R1", "value": "1k"},
                   {"lib_id": "Device:R", "ref": "R2", "value": "1k"}],
         "shunts": {"1": [{"lib_id": "Device:C", "ref": "C1", "value": "1n", "to": "GND"},
                          {"lib_id": "Device:C", "ref": "C2", "value": "1n", "to": "GND"}],
                    "2": [{"lib_id": "Device:C", "ref": "C3", "value": "1n", "to": "GND"}]},
         "nets": {"0": "IN", "2": "OUT"}},
        {"op": "pwr_flag", "pin": "C1.2"},
        {"op": "symbol", "lib_id": "Device:R", "ref": "R3", "value": "1k",
         "x_mm": 50.8, "y_mm": 88.9, "rotation": 90},
        {"op": "symbol", "lib_id": "Device:R", "ref": "R4", "value": "1k",
         "x_mm": 50.8, "y_mm": 96.52, "rotation": 90},
        {"op": "connect", "pins": ["R3.2", "R4.2"], "net": "+5V"},
        {"op": "join_power", "pins": ["R3.2", "R4.2"], "net": "+5V"},
        {"op": "pwr_flag", "pin": "R3.2"},
        {"op": "connect", "pins": ["R3.1", "R4.1"], "net": "IN", "join": False},
        {"op": "move_region", "x1_mm": 40.64, "y1_mm": 83.82, "x2_mm": 63.5, "y2_mm": 101.6,
         "dx_mm": 0, "dy_mm": 12.7}])
    ours = _nets(mcp)
    sch = state.get_active().sch_path
    out = tmp_path / "erc.json"
    subprocess.run([str(cli), "sch", "erc", "--format", "json", "-o", str(out), str(sch)],
                   check=True, capture_output=True)
    import json
    report = json.loads(out.read_text(encoding="utf-8"))
    violations = [v for s in report["sheets"] for v in s["violations"]]
    assert not violations, json.dumps(violations)
    net = tmp_path / "n.net"
    subprocess.run([str(cli), "sch", "export", "netlist", "--format", "kicadsexpr",
                    "-o", str(net), str(sch)], check=True, capture_output=True)
    from kicad_claude.adapters import kicad_python
    theirs = {n["name"].lstrip("/"): sorted(f"{r}.{p}" for r, p in n["nodes"])
              for n in kicad_python.netlist_pad_map(net)["nets"]}
    for name in ("IN", "OUT", "GND", "+5V"):
        assert theirs[name] == [p for p in ours[name] if not p.startswith("#")], name
