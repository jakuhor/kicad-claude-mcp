"""DC operating-point model (OP-7): resistor networks, pin clamps,
zeners, switch scenarios."""

from __future__ import annotations

import pytest

from kicad_claude.adapters import connectivity_review, dc_model
from kicad_claude.adapters import datasheet_specs as dss
from kicad_claude.adapters import power_review as pr

from .test_review_ratings_circuits_bom import _comp, _net, _nl


def _sv(**kw):
    return {"unit": kw.pop("unit"), "page": 1, **kw}


class TestNetworkAndClamps:
    def test_divider_with_pin_clamp_and_trip(self, tmp_path):
        # 20 V -> 90.9k / 8.06k -> pin clamped at 1.37 V (open circuit 1.63 V)
        comps = [_comp("U1", "REG"), _comp("R1", "90.9k"), _comp("R2", "8.06k")]
        nets = [_net(1, "VIN", ("R1", "1", "", "passive"), ("U1", "1", "VIN", "power_in")),
                _net(2, "OVUV", ("R1", "2", "", "passive"), ("R2", "1", "", "passive"),
                     ("U1", "5", "OV/UV", "input")),
                _net(3, "GND", ("R2", "2", "", "passive"), ("U1", "9", "GND", "power_in"))]
        nl = _nl(tmp_path, comps, nets)
        specs = {"U1": {"mpn": "REG1", "pin_models": {"OV/UV": {
            "clamp": _sv(typ=1.37, unit="V"), "i_trip": _sv(typ=80e-6, unit="A")}}}}
        res = dc_model.solve(nl, {"VIN": (20.0, "caller")}, specs)
        assert res["volts"]["OVUV"][0] == pytest.approx(1.37)
        c = res["clamps"][0]
        # (20 - 1.37) / 90.9k - 1.37 / 8.06k = 35 uA
        assert c["current_a"] == pytest.approx((20 - 1.37) / 90.9e3 - 1.37 / 8.06e3, rel=1e-6)
        assert not c["tripped"]
        # trip at Vin = 1.37 + 90.9k * (80u + 1.37 / 8.06k)
        assert c["source_v_at_trip"] == pytest.approx(1.37 + 90.9e3 * (80e-6 + 1.37 / 8.06e3))
        assert dc_model.findings(res)[0]["rule"] == "DC-CLAMP"
        # without the clamp model: plain divider
        plain = dc_model.solve(nl, {"VIN": (20.0, "caller")}, {})
        assert plain["volts"]["OVUV"][0] == pytest.approx(20 * 8.06 / 98.96, rel=1e-6)

    def test_bias_current_and_multi_branch(self, tmp_path):
        # node fed by 10k from 5 V, 10k to ground, 10k to 3.3 V, 1 uA out of the EN pin
        comps = [_comp("U1", "BUCK"), _comp("R1", "10k"), _comp("R2", "10k"), _comp("R3", "10k")]
        nets = [_net(1, "5V", ("R1", "1", "", "passive")),
                _net(2, "3V3", ("R3", "1", "", "passive")),
                _net(3, "EN", ("R1", "2", "", "passive"), ("R2", "1", "", "passive"),
                     ("R3", "2", "", "passive"), ("U1", "4", "EN", "input")),
                _net(4, "GND", ("R2", "2", "", "passive"))]
        nl = _nl(tmp_path, comps, nets)
        specs = {"U1": {"mpn": "B", "regulator": {"en_pullup_current": _sv(typ=1e-6, unit="A")}}}
        res = dc_model.solve(nl, {"5V": (5.0, "c"), "3V3": (3.3, "c")}, specs)
        # (5 + 3.3) / 3 + 1 uA * 10k / 3
        assert res["volts"]["EN"][0] == pytest.approx(8.3 / 3 + 1e-6 * 1e4 / 3, rel=1e-6)

    def test_pin_model_schema(self):
        good = {"category": "regulator_switching",
                "pin_models": {"OV/UV": {"clamp": _sv(typ=1.37, unit="V")}}}
        assert dss.validate(good, ["1.37"])[0] == []
        bad = {"category": "regulator_switching",
               "pin_models": {"OV/UV": {"clamp": _sv(typ=1.37, unit="A"), "x": {}}}}
        assert dss.validate(bad, ["1.37"])[0]


def _pfet(tmp_path):
    """USB-PD style high-side P-FET: gate pulled to VBUS (10k) and to source
    (49.9k), driven low through 1k by U1, 12 V zener gate-source."""
    comps = [_comp("Q1", "PFET", lib="Device:Q_PMOS_GSD"), _comp("R1", "10k"),
             _comp("R2", "49.9k"), _comp("R3", "1k"), _comp("D1", "12V", lib="Diode:BZX84Cxx"),
             _comp("U1", "PDC"), _comp("C1", "10u", {"Voltage": "16V"})]
    nets = [_net(1, "VBUS", ("Q1", "3", "S", "passive"), ("R1", "2", "", "passive"),
                 ("R2", "2", "", "passive"), ("D1", "3", "K", "passive"),
                 ("U1", "1", "VBUS", "power_in")),
            _net(2, "GATE", ("Q1", "1", "G", "input"), ("R1", "1", "", "passive"),
                 ("R2", "1", "", "passive"), ("R3", "2", "", "passive"),
                 ("D1", "1", "A", "passive")),
            _net(3, "DRV", ("R3", "1", "", "passive"), ("U1", "2", "EN_OUT", "open_collector")),
            _net(4, "VOUT", ("Q1", "2", "D", "passive"), ("C1", "1", "", "passive")),
            _net(5, "GND", ("C1", "2", "", "passive"), ("U1", "9", "GND", "power_in"))]
    return _nl(tmp_path, comps, nets)


class TestSwitches:
    def test_on_off_and_zener(self, tmp_path):
        nl = _pfet(tmp_path)
        known = {"VBUS": (20.0, "caller")}
        assert dc_model.polarity(nl, "Q1", {}) == "P"
        on = dc_model.solve(nl, known, {}, {"Q1": "on"})
        assert on["volts"]["VOUT"][0] == pytest.approx(20.0)
        # zener holds VBUS - GATE at 12 V; the driver node sits at 0 V
        assert on["volts"]["GATE"][0] == pytest.approx(8.0)
        z = next(c for c in on["clamps"] if c["kind"] == "zener")
        i_r3 = 8.0 / 1e3
        assert z["current_a"] == pytest.approx(i_r3 - 12 / 10e3 - 12 / 49.9e3, rel=1e-6)
        drv = on["drivers"][0]
        assert drv["level"] == "low" and drv["current_a"] == pytest.approx(-i_r3)
        off = dc_model.solve(nl, known, {}, {"Q1": "off"})
        assert off["volts"]["VOUT"][0] == 0.0            # P body diode D -> S: no path
        assert off["volts"]["GATE"][0] == pytest.approx(20.0)   # driver released
        assert off["drivers"][0]["level"] == "released"

    def test_cap_voltage_per_scenario(self, tmp_path):
        nl = _pfet(tmp_path)
        known = {"VBUS": (20.0, "caller")}
        on = dc_model.solve(nl, known, {}, {"Q1": "on"})
        f: list = []
        connectivity_review.check_cap_voltage(nl, f, {**on["volts"], **known})
        assert any(x["rule"] == "CR-CAP-V" and x["ref"] == "C1" for x in f)   # 20 V on 16 V
        off = dc_model.solve(nl, known, {}, {"Q1": "off"})
        f = []
        connectivity_review.check_cap_voltage(nl, f, {**off["volts"], **known})
        assert not f

    def test_power_tree_uses_model(self, tmp_path):
        comps = [_comp("U1", "LDO"), _comp("R1", "90.9k"), _comp("R2", "8.06k")]
        nets = [_net(1, "VIN", ("R1", "1", "", "passive"), ("U1", "1", "VIN", "power_in")),
                _net(2, "ENN", ("R1", "2", "", "passive"), ("R2", "1", "", "passive"),
                     ("U1", "5", "EN", "input")),
                _net(3, "VOUT", ("U1", "2", "VOUT", "power_out")),
                _net(4, "GND", ("R2", "2", "", "passive"), ("U1", "9", "GND", "power_in"))]
        nl = _nl(tmp_path, comps, nets)
        specs = {"U1": {"mpn": "L1", "pin_models": {"EN": {"clamp": _sv(typ=1.37, unit="V")}}}}
        r = pr.analyze(nl, rail_voltages={"VIN": 20}, specs=specs)
        assert r["net_voltages"]["ENN"]["volts"] == pytest.approx(1.37)
        assert any(f["rule"] == "DC-CLAMP" for f in r["findings"])
        assert "net_voltages" not in pr.analyze(nl, rail_voltages={"VIN": 20})
