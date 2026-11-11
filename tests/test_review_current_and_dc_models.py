"""Current model (OP-9: non-buck ripple, DCM, efficiency and VF
curves, input pin / internal outputs) and DC model (OP-10: body-diode VSD,
current-limited drivers, internal-LDO output level)."""

from __future__ import annotations

import pytest

from kicad_claude.adapters import dc_model, part_ratings
from kicad_claude.adapters import power_review as pr

from .test_review_ratings_circuits_bom import _comp, _net, _nl


def _sv(**kw):
    return {"unit": kw.pop("unit"), "page": 1, **kw}


def _boost(tmp_path):
    comps = [_comp("J1", "PWR"), _comp("U1", "BOOST"), _comp("L1", "10u"), _comp("D1", "SS14"),
             _comp("R1", "90k"), _comp("R2", "10k"), _comp("C1", "10u"), _comp("C2", "22u")]
    nets = [
        _net(1, "VIN", ("J1", "1", "VIN", "passive"), ("J1", "2", "VIN", "passive"),
             ("U1", "1", "VIN", "power_in"), ("L1", "1", "", "passive"),
             ("C1", "1", "", "passive")),
        _net(2, "GND", ("J1", "3", "GND", "passive"), ("U1", "9", "GND", "power_in"),
             ("R2", "2", "", "passive"), ("C1", "2", "", "passive"), ("C2", "2", "", "passive")),
        _net(3, "SW", ("U1", "2", "SW", "input"), ("L1", "2", "", "passive"),
             ("D1", "2", "A", "passive")),
        _net(4, "+12V", ("D1", "1", "K", "passive"), ("R1", "1", "", "passive"),
             ("C2", "1", "", "passive")),
        _net(5, "FB", ("U1", "3", "FB", "input"), ("R1", "2", "", "passive"),
             ("R2", "1", "", "passive")),
    ]
    return _nl(tmp_path, comps, nets)


class TestRipple:
    def test_topology_and_duty(self):
        i = {"carries": "iin", "buck": False}
        o = {"carries": "iout", "buck": False}
        g = {"carries": "gnd", "buck": False}
        assert part_ratings.stage_topology([i], 5, 12) == ("boost", pytest.approx(1 - 5 / 12))
        assert part_ratings.stage_topology([i, o], 20, -20) == ("cuk", pytest.approx(0.5))
        assert part_ratings.stage_topology([g, o], 12, 5)[0] == "sepic/zeta"
        assert part_ratings.stage_topology([g], 12, -5) == ("inverting buck-boost",
                                                            pytest.approx(5 / 17))
        assert part_ratings.stage_topology([{"carries": "iout", "buck": True}], 12, 5) == \
            ("buck", pytest.approx(5 / 12))

    def test_boost_ccm_and_dcm(self, tmp_path):
        nl = _boost(tmp_path)
        pt = pr.analyze(nl, vref={"U1": 1.2}, rail_voltages={"VIN": 5})
        specs = {"U1": {"mpn": "B1", "regulator": {"fsw": _sv(typ=1e6, unit="Hz")},
                        "electrical": {"ISW_LIMIT": _sv(min=1.0, unit="A")}}}
        r = part_ratings.check(nl, pt, specs, {"+12V": 0.5}, efficiency={"U1": 0.9})
        x = r["inductors"]["L1"]
        d = 1 - 5 / 12
        assert x["topology"] == "boost" and x["mode"] == "CCM"
        assert x["ripple_pp"] == pytest.approx(5 * d / (10e-6 * 1e6), rel=1e-3)
        iin = 12 * 0.5 / (0.9 * 5)
        assert x["i_avg"] == pytest.approx(iin, rel=1e-3)
        assert x["i_peak"] == pytest.approx(iin + x["ripple_pp"] / 2, rel=1e-3)
        assert any(f["rule"] == "RT-REG-ILIM" for f in r["findings"])     # 1.45 A > 1 A
        light = part_ratings.check(nl, pt, specs, {"+12V": 0.01}, efficiency={"U1": 0.9})
        y = light["inductors"]["L1"]
        assert y["mode"] == "DCM"
        assert y["i_peak"] == pytest.approx((2 * y["i_avg"] * y["ripple_pp"]) ** 0.5, rel=1e-3)

    def test_fccm_stays_ccm(self, tmp_path):
        nl = _boost(tmp_path)
        pt = pr.analyze(nl, vref={"U1": 1.2}, rail_voltages={"VIN": 5})
        specs = {"U1": {"mpn": "B1", "regulator": {"fsw": _sv(typ=1e6, unit="Hz"),
                                                    "light_load": "fccm"}}}
        r = part_ratings.check(nl, pt, specs, {"+12V": 0.01}, efficiency={"U1": 0.9})
        assert r["inductors"]["L1"]["mode"].startswith("CCM")


class TestCurves:
    def test_efficiency_curve_choice(self):
        rec = {"mpn": "R", "curves": {
            "efficiency_a": {"x_unit": "A", "y_unit": "%", "page": 3, "conditions":
                             "VIN = 12 V, VOUT = 5 V", "points": [[0.1, 80], [1, 90]]},
            "efficiency_b": {"x_unit": "A", "y_unit": "%", "page": 3, "conditions":
                             "VIN = 24 V, VOUT = 5 V", "points": [[0.1, 70], [1, 86]]}}}
        eta, src = part_ratings.efficiency_from_curves(rec, 20, 5, 0.55)
        assert eta == pytest.approx(0.78) and "efficiency_b" in src
        eta, src = part_ratings.efficiency_from_curves(rec, 12, 5, 5.0)
        assert eta == pytest.approx(0.90) and "outside the curve" in src

    def test_led_vf_curve(self, tmp_path):
        comps = [_comp("D1", "RED", lib="Device:LED"), _comp("R1", "1k")]
        nets = [_net(1, "+5V", ("R1", "1", "", "passive")),
                _net(2, "A", ("R1", "2", "", "passive"), ("D1", "2", "A", "passive")),
                _net(3, "GND", ("D1", "1", "K", "passive"))]
        nl = _nl(tmp_path, comps, nets)
        specs = {"D1": {"mpn": "L", "category": "led", "curves": {"VF": {
            "x_unit": "A", "y_unit": "V", "page": 3, "points": [[0.001, 1.8], [0.005, 2.0]]}}}}
        f: list = []
        rows = part_ratings.check_leds(nl, specs, {"+5V": 5.0}, f, lambda *a: None)
        i = rows[0]["current"]
        vf = 1.8 + (2.0 - 1.8) * (i - 0.001) / 0.004 if i < 0.005 else 2.0
        assert i == pytest.approx((5 - vf) / 1e3, abs=1e-5)
        assert rows[0]["vf_at_current"] == pytest.approx(vf, abs=0.002)   # ~1.9 V at 3 mA


class TestPowerTreeSpecPins:
    def test_input_pin_and_internal_output(self, tmp_path):
        comps = [_comp("U1", "DUAL"), _comp("L1", "10u"), _comp("R1", "90k"), _comp("R2", "10k"),
                 _comp("C1", "1u")]
        nets = [_net(1, "VBUS", ("U1", "5", "C2", "passive"), ("C1", "1", "", "passive")),
                _net(2, "SH", ("U1", "6", "SHOUT", "power_out"), ("U1", "1", "VIN", "power_in")),
                _net(3, "SWN", ("U1", "2", "SW", "output"), ("L1", "1", "", "passive")),
                _net(4, "OUT", ("L1", "2", "", "passive"), ("R1", "1", "", "passive")),
                _net(5, "FB", ("U1", "3", "FB", "input"), ("R1", "2", "", "passive"),
                     ("R2", "1", "", "passive")),
                _net(6, "GND", ("R2", "2", "", "passive"), ("C1", "2", "", "passive"),
                     ("U1", "9", "GND", "power_in"))]
        nl = _nl(tmp_path, comps, nets)
        ds = {"U1": {"input_pin": ("C2", "ds"), "internal_outputs": {"SHOUT": ("C2", 4.25, "ds")}}}
        r = pr.analyze(nl, vref={"U1": 0.8}, datasheet=ds, rail_voltages={"VBUS": 20})
        reg = r["regulators"][0]
        assert reg["input_net"] == "VBUS" and "C2" in reg["input_note"]
        assert r["rail_voltages"]["SH"]["volts"] == pytest.approx(24.25)


def _pfet_drv(tmp_path, with_ldo=False):
    comps = [_comp("Q1", "PFET", lib="Device:Q_PMOS_GSD"), _comp("R1", "10k"),
             _comp("R2", "1k"), _comp("U1", "PDC"), _comp("Q2", "NFET", lib="Device:Q_NMOS_GSD"),
             _comp("R3", "100k")]
    nets = [_net(1, "VBUS", ("Q1", "3", "S", "passive"), ("R1", "2", "", "passive"),
                 ("U1", "1", "VBUS", "power_in")),
            _net(2, "GATE", ("Q1", "1", "G", "input"), ("R1", "1", "", "passive"),
                 ("R2", "2", "", "passive")),
            _net(3, "DRV", ("R2", "1", "", "passive"), ("U1", "2", "EN_OUT", "output")),
            _net(4, "VOUT", ("Q1", "2", "D", "passive")),
            _net(5, "NG", ("Q2", "1", "G", "input"), ("U1", "3", "GPIO", "output"),
                 ("R3", "1", "", "passive")),
            _net(6, "GND", ("Q2", "2", "S", "passive"), ("R3", "2", "", "passive"),
                 ("U1", "9", "GND", "power_in")),
            _net(7, "LED", ("Q2", "3", "D", "passive")),
            _net(8, "VDDD", ("U1", "4", "VDDD", "power_out"))]
    return _nl(tmp_path, comps, nets)


class TestDcModelOp10:
    def test_current_limited_driver(self, tmp_path):
        nl = _pfet_drv(tmp_path)
        specs = {"U1": {"mpn": "PDC1", "pin_models": {"EN_OUT": {"i_sink": _sv(min=1e-3,
                                                                                unit="A")}}}}
        res = dc_model.solve(nl, {"VBUS": (20.0, "c")}, specs, {"Q1": "on"})
        d = next(x for x in res["drivers"] if x["pin"] == "EN_OUT")
        assert d["current_limited"] and d["current_wanted_a"] == pytest.approx(20 / 11e3)
        # gate = 20 V - 1 mA * 10k
        assert res["volts"]["GATE"][0] == pytest.approx(10.0)
        f = [x for x in dc_model.findings(res) if x["rule"] == "DC-DRIVE"]
        assert f and f[0]["severity"] == "warning"

    def test_body_diode_vsd(self, tmp_path):
        nl = _pfet_drv(tmp_path)
        # P-FET body diode is drain -> source: VOUT (drain) unknown, VBUS known on source
        specs = {"Q1": {"mpn": "F", "electrical": {"VSD": _sv(typ=0.7, unit="V")}}}
        off = dc_model.solve(nl, {"VOUT": (20.0, "c")}, specs, {"Q1": "off"})
        assert off["volts"]["VBUS"][0] == pytest.approx(19.3)

    def test_ldo_output_level(self, tmp_path):
        nl = _pfet_drv(tmp_path)
        specs = {"U1": {"mpn": "PDC1", "pin_models": {"VDDD": {"v_out": _sv(typ=3.3, unit="V")}}}}
        on = dc_model.solve(nl, {"VBUS": (20.0, "c")}, specs, {"Q2": "on"})
        assert on["volts"]["NG"][0] == pytest.approx(3.3)       # high from the internal LDO
        no = dc_model.solve(nl, {"VBUS": (20.0, "c")}, {}, {"Q2": "on"})
        assert next(x for x in no["drivers"] if x["pin"] == "GPIO")["level"] == "released"
        voh = {"U1": {"mpn": "P", "pin_models": {"GPIO": {"v_oh": _sv(min=2.7, unit="V")}}}}
        on2 = dc_model.solve(nl, {"VBUS": (20.0, "c")}, voh, {"Q2": "on"})
        assert on2["volts"]["NG"][0] == pytest.approx(2.7)
