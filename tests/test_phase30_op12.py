"""Phase 30 — OP-12: two-inductor DCM, coupled inductors, IC-only loss,
efficiency curves far from the stage, DC-model missing-data report."""

from __future__ import annotations

import pytest

from kicad_claude.adapters import dc_model, part_ratings
from kicad_claude.adapters import power_review as pr

from .test_phase25_ratings_circuits_bom import _comp, _net, _nl


def _sv(**kw):
    return {"unit": kw.pop("unit"), "page": 1, **kw}


def _sepic(tmp_path, coupled=False):
    """SEPIC 12 V -> 12 V: L1 input -> SW, C2 SW -> N, L2 N -> GND (here the
    output-side inductor goes to the output through D1; Cuk-like counting is
    what matters: one input-side and one output-side inductor)."""
    if coupled:
        comps = [_comp("U1", "SEPIC"), _comp("L1", "22u"), _comp("D1", "SS14"),
                 _comp("C2", "1u"), _comp("R1", "140k"), _comp("R2", "10k"), _comp("C3", "22u")]
        lnodes = [("L1", "1", "", "passive"), ("L1", "2", "", "passive"),
                  ("L1", "3", "", "passive"), ("L1", "4", "", "passive")]
        nets = [
            _net(1, "VIN", ("U1", "1", "VIN", "power_in"), lnodes[0]),
            _net(2, "SW", ("U1", "2", "SW", "output"), lnodes[1], ("C2", "1", "", "passive")),
            _net(3, "N", ("C2", "2", "", "passive"), lnodes[2], ("D1", "2", "A", "passive")),
            _net(4, "OUT", lnodes[3], ("R1", "1", "", "passive"), ("C3", "1", "", "passive")),
            _net(5, "FB", ("U1", "3", "FB", "input"), ("R1", "2", "", "passive"),
                 ("R2", "1", "", "passive")),
            _net(6, "GND", ("R2", "2", "", "passive"), ("C3", "2", "", "passive"),
                 ("U1", "9", "GND", "power_in"), ("D1", "1", "K", "passive"))]
        return _nl(tmp_path, comps, nets)
    comps = [_comp("U1", "SEPIC"), _comp("L1", "22u"), _comp("L2", "22u"), _comp("D1", "SS14"),
             _comp("C2", "1u"), _comp("R1", "140k"), _comp("R2", "10k"), _comp("C3", "22u")]
    nets = [
        _net(1, "VIN", ("U1", "1", "VIN", "power_in"), ("L1", "1", "", "passive")),
        _net(2, "SW", ("U1", "2", "SW", "output"), ("L1", "2", "", "passive"),
             ("C2", "1", "", "passive")),
        _net(3, "N", ("C2", "2", "", "passive"), ("L2", "1", "", "passive"),
             ("D1", "2", "A", "passive")),
        _net(4, "OUT", ("L2", "2", "", "passive"), ("R1", "1", "", "passive"),
             ("C3", "1", "", "passive")),
        _net(5, "FB", ("U1", "3", "FB", "input"), ("R1", "2", "", "passive"),
             ("R2", "1", "", "passive")),
        _net(6, "GND", ("R2", "2", "", "passive"), ("C3", "2", "", "passive"),
             ("U1", "9", "GND", "power_in"), ("D1", "1", "K", "passive"))]
    return _nl(tmp_path, comps, nets)


SPECS = {"U1": {"mpn": "S1", "regulator": {"fsw": _sv(typ=500e3, unit="Hz")},
                "thermal": {"rth_ja": _sv(typ=50, unit="C/W"), "tj_max": _sv(max=150, unit="C")}},
         "L1": {"mpn": "IND", "electrical": {"DCR": _sv(typ=0.1, unit="Ohm")}},
         "L2": {"mpn": "IND", "electrical": {"DCR": _sv(typ=0.1, unit="Ohm")}},
         "D1": {"mpn": "DIO", "electrical": {"VF": _sv(typ=0.5, unit="V")}}}


def _check(tmp_path, load, coupled=False):
    nl = _sepic(tmp_path, coupled)
    pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VIN": 12})
    return part_ratings.check(nl, pt, SPECS, {"OUT": load}, efficiency={"U1": 0.85},
                              ambient_c=25)


class TestTwoInductorStage:
    def test_ccm_then_dcm(self, tmp_path):
        r = _check(tmp_path, 1.0)
        rows = [r["inductors"][k] for k in ("L1", "L2")]
        assert all(x["mode"] == "CCM" for x in rows)
        assert rows[0]["ripple_pp"] == pytest.approx(12 * 0.5 / (22e-6 * 500e3), rel=1e-3)
        light = _check(tmp_path, 0.01)
        rows = [light["inductors"][k] for k in ("L1", "L2")]
        assert all(x["mode"] == "DCM" for x in rows)
        isum = sum(x["i_avg"] for x in rows)
        dsum = sum(x["ripple_pp_ccm"] for x in rows)
        assert light["regulators"]["U1"]["switch_peak"] == pytest.approx(
            (2 * isum * dsum) ** 0.5, rel=1e-3)
        assert rows[0]["ripple_pp"] < rows[0]["ripple_pp_ccm"]

    def test_coupled_halves_ripple(self, tmp_path):
        r = _check(tmp_path, 1.0, coupled=True)
        w = r["inductors"]
        assert set(w) >= {"L1.1", "L1.2"} and w["L1.1"].get("coupled")
        assert w["L1.1"]["ripple_pp"] == pytest.approx(12 * 0.5 / (22e-6 * 500e3) / 2, rel=1e-3)


class TestIcLoss:
    def test_external_losses_subtracted(self, tmp_path):
        r = _check(tmp_path, 1.0)
        io = r["regulators"]["U1"]
        assert io["p_external"] > 0.5 * 1.0          # D1: 0.5 V x 1 A, plus DCR
        assert io["p_ic"] == pytest.approx(io["p_loss"] - io["p_external"], abs=2e-4)
        row = next(t for t in r["thermal"] if t["ref"] == "U1")
        assert "D1 VF x I" in row["what"] and row["p_w"] == pytest.approx(io["p_ic"], abs=1e-4)

    def test_far_curve_flag(self):
        rec = {"mpn": "R", "curves": {"efficiency": {
            "x_unit": "A", "y_unit": "%", "page": 3, "conditions": "VIN = 18 V, VOUT = 12 V",
            "points": [[0.1, 76], [1, 86]]}}}
        _, src = part_ratings.efficiency_from_curves(rec, 20, 20, 0.15)
        assert "FAR from this stage" in src
        _, src = part_ratings.efficiency_from_curves(rec, 18, 12, 0.15)
        assert "FAR" not in src


def test_dc_missing_data(tmp_path):
    comps = [_comp("Q1", "FET"), _comp("R1", "10k"), _comp("U1", "CTRL")]
    nets = [_net(1, "VIN", ("Q1", "3", "S", "passive"), ("R1", "2", "", "passive")),
            _net(2, "G", ("Q1", "1", "G", "input"), ("R1", "1", "", "passive"),
                 ("U1", "2", "EN", "output")),
            _net(3, "OUT", ("Q1", "2", "D", "passive"))]
    nl = _nl(tmp_path, comps, nets)
    res = dc_model.solve(nl, {"VIN": (12.0, "c")}, {}, {"Q1": "off"})
    assert res["missing_data"]["switches_without_polarity"] == ["Q1"]
    f = [x for x in dc_model.findings(res) if x["rule"] == "DC-NODATA"]
    assert f and "unknown P / N" in f[0]["message"]
