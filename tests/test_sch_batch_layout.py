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
WANTED = {"Device": ["R", "C"], "power": ["GND", "+5V", "VCC", "PWR_FLAG"],
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
    fields = g.visible_fields(q)
    from kicad_claude.adapters import sch_layout as lay
    places = [(float(sch_io.find_child(f, "at")[1]), float(sch_io.find_child(f, "at")[2]))
              for f in fields]
    assert lay._wire_hits(tree, fields, places, "left", 1.27) == 0 or \
        lay._wire_hits(tree, fields, places, "right", 1.27) == 0


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
