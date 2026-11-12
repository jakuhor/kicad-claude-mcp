"""Board thermal review (OP-17 B3 / B4): Tj with the layout's thermal vias,
hot-spot proximity."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kicad_claude.adapters import board_thermal as bt
from kicad_claude.adapters import sch_io


def _qfn(x, y, n_vias, drill=0.3):
    sig = " ".join(f'(pad "{i}" smd rect (at {-1.5 + 0.5 * (i % 4)} {-1.5 if i < 8 else 1.5}) '
                   f'(size 0.25 0.8) (layers "F.Cu") (net "S{i}"))' for i in range(16))
    ep = '(pad "17" smd rect (at 0 0) (size 1.7 1.7) (layers "F.Cu") (net "GND"))'
    fp = (f'(footprint "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm" (layer "F.Cu") '
          f'(at {x} {y}) (property "Reference" "U1") {sig} {ep})')
    grid = [(x + dx, y + dy) for dx in (-0.6, 0, 0.6) for dy in (-0.6, 0, 0.6)][:n_vias]
    vs = " ".join(f'(via (at {vx} {vy}) (size 0.6) (drill {drill}) (layers "F.Cu" "B.Cu") '
                  f'(net "GND"))' for vx, vy in grid)
    return fp + " " + vs


def _cap(ref, x, y, lib="Capacitor_SMD:C_0603_1608Metric"):
    return (f'(footprint "{lib}" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}") '
            f'(pad "1" smd rect (at -0.8 0) (size 0.8 0.9) (layers "F.Cu") (net "V")) '
            f'(pad "2" smd rect (at 0.8 0) (size 0.8 0.9) (layers "F.Cu") (net "GND")))')


def _board(tmp_path, n_vias, extra=""):
    text = ('(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal) (2 "B.Cu" signal)) '
            + _qfn(20, 20, n_vias) + " " + extra + ")")
    p = tmp_path / "t.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return sch_io.parse_file(p)


def _nl(caps=()):
    comps = {"U1": {"value": "BUCK", "fields": {}}}
    for ref, value, desc in caps:
        comps[ref] = {"value": value, "fields": {}, "description": desc, "footprint": "",
                      "lib_id": "Device:C"}
    return SimpleNamespace(components=comps)


RATINGS = {"dissipation": [{"ref": "U1", "p_w": 1.0, "method": "test"}]}


def _run(tree, specs=None, nl=None, tj_margin=25.0):
    return bt.analyze(tree, nl or _nl(), specs or {}, RATINGS, 25.0, tj_margin)


def test_thermal_vias_correct_the_package_estimate(tmp_path):
    none = _run(_board(tmp_path, 0))
    row = none["parts"][0]
    assert row["rth_ja"] == 50 and row["rth_source"] == "package_typical (assumed)"
    assert row["exposed_pads"][0]["adequacy"] == "none" and row["tj_c"] == pytest.approx(75)
    assert ("TH-VIAS", "U1") in {(f["rule"], f["ref"]) for f in none["findings"]}
    good = _run(_board(tmp_path, 9))
    row = good["parts"][0]
    assert row["exposed_pads"][0]["vias"] == 9 and row["exposed_pads"][0]["adequacy"] == "good"
    assert row["board_correction"] == 0.5 and row["tj_c"] == pytest.approx(50)
    assert not [f for f in good["findings"] if f["rule"] == "TH-VIAS"]


def test_datasheet_rth_wins_and_table_is_capped(tmp_path):
    tree = _board(tmp_path, 9)
    ds = {"U1": {"mpn": "BUCK1", "thermal": {"rth_ja": {"typ": 60, "unit": "C/W", "page": 2},
                                             "tj_max": {"max": 80, "unit": "C", "page": 2}}}}
    r = _run(tree, ds)
    row = r["parts"][0]
    assert row["rth_source"] == "datasheet BUCK1 p.2" and row["board_correction"] == 1.0
    assert [(f["rule"], f["severity"]) for f in r["findings"]] == [("TH-TJ", "error")]  # 85 > 80
    # package table, no vias: 75 C > 60 C is only a warning
    tab = _run(_board(tmp_path, 0), {"U1": {"mpn": "BUCK1", "thermal": {
        "tj_max": {"max": 60, "unit": "C", "page": 2}}}})
    tj = [f for f in tab["findings"] if f["rule"] == "TH-TJ"]
    assert [f["severity"] for f in tj] == ["warning"] and "estimate" in tj[0]["message"]
    # no Tj max at all: TH-NODATA
    assert ("TH-NODATA", "U1") in {(f["rule"], f["ref"]) for f in _run(tree)["findings"]}


def test_hot_spot_proximity(tmp_path):
    caps = [("C1", "10u X7R", ""), ("C2", "10u X7R", ""), ("C3", "100u", "electrolytic"),
            ("C4", "1n C0G", "")]
    extra = " ".join([_cap("C1", 25, 20), _cap("C2", 35, 20),
                      _cap("C3", 20, 25, "Capacitor_SMD:CP_Elec_6.3x5.8"), _cap("C4", 20, 15)])
    r = _run(_board(tmp_path, 0, extra), nl=_nl(caps))      # U1 rise 50 K
    got = {(f["rule"], f["ref"]) for f in r["findings"]}
    assert ("TH-MLCC-HOT", "C1") in got                       # 5 mm
    assert ("TH-MLCC-HOT", "C2") not in got                   # 15 mm
    assert ("TH-ELCO-HOT", "C3") in got
    assert not any(ref == "C4" for _, ref in got)             # C0G: no drift
    cool = _run(_board(tmp_path, 9, extra), {"U1": {"mpn": "B", "thermal": {
        "rth_ja": {"typ": 15, "unit": "C/W", "page": 1}}}}, _nl(caps))      # rise 15 K
    assert not [f for f in cool["findings"] if f["rule"].endswith("-HOT")]


def test_analyze_board_thermal_tool(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    from kicad_claude import state
    from kicad_claude.tools.emc_si import thermal
    from tests.test_review_ratings_circuits_bom import _buck

    _buck(tmp_path)                                  # writes s.net: buck U1 -> +5V
    _board(tmp_path, 0)
    monkeypatch.setattr(state, "get_active_board_path", lambda: tmp_path / "t.kicad_pcb")
    mcp = FastMCP("t")
    thermal.register(mcp)
    fn = mcp._tool_manager.get_tool("analyze_board_thermal").fn
    r = fn(loads={"+5V": 3.0}, rail_voltages={"VIN": 12, "+5V": 5},
           netlist_path=str(tmp_path / "s.net"))
    got = {(f["rule"], f["ref"]) for f in r["findings"]}
    assert ("TH-VIAS", "U1") in got and ("TH-NODATA", "U1") in got
    u1 = next(p for p in r["parts"] if p["ref"] == "U1")
    assert u1["rth_source"] == "package_typical (assumed)"
    assert r["assumptions"]["ambient_source"].startswith("default")
