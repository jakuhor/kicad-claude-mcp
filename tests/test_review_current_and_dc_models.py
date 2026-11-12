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


class TestUnknownSupplies:
    """Field report (firbox base_board vs kicad-happy, 2026-11-12): a rail fed
    through a load switch from a USB-PD contract has no known voltage; tied to
    ground only through an EN divider it solved to a few mV and failed every
    VIN / EN check, and an LED string walked across it through a TVS."""

    def _board(self, tmp_path):
        comps = [_comp("U1", "BUCK"), _comp("R1", "1430k"), _comp("R2", "100k"),
                 _comp("J1", "USB_C", lib="Connector:USB_C"), _comp("D6", "SMF24A"),
                 _comp("D3", "RED", lib="Device:LED"), _comp("D8", "CCR"), _comp("C1", "1u")]
        nets = [_net(1, "VBUS", ("J1", "1", "VBUS", "passive"), ("D6", "1", "K", "passive"),
                     ("D8", "1", "A", "passive"), ("C1", "1", "", "passive")),
                _net(2, "VIN", ("U1", "1", "VIN", "power_in"), ("R1", "1", "", "passive")),
                _net(3, "EN", ("U1", "2", "EN", "input"), ("R1", "2", "", "passive"),
                     ("R2", "1", "", "passive")),
                _net(4, "LA", ("D8", "2", "K", "passive"), ("D3", "2", "A", "passive")),
                _net(5, "GND", ("U1", "9", "GND", "power_in"), ("R2", "2", "", "passive"),
                     ("D6", "2", "A", "passive"), ("D3", "1", "K", "passive"),
                     ("C1", "2", "", "passive"))]
        return _nl(tmp_path, comps, nets)

    def test_unsourced_rail_stays_unknown(self, tmp_path):
        nl = self._board(tmp_path)
        specs = {"U1": {"mpn": "B", "regulator": {"en_pullup_current": {
            "typ": 1e-6, "unit": "A", "page": 1}}}}
        volts = dc_model.solve(nl, {}, specs)["volts"]
        assert "VIN" not in volts and "EN" not in volts
        # with the rail given, the divider solves as before
        volts = dc_model.solve(nl, {"VIN": (20.0, "caller")}, specs)["volts"]
        assert volts["EN"][0] == pytest.approx(20 * 100 / 1530 + 1e-6 * 1430e3 * 100 / 1530,
                                               rel=1e-6)

    def test_unknown_rail_with_divider_tap_pulled_to_a_known_rail(self, tmp_path):
        # OP-20 #1: EN tap also pulled to +3V3 (a PG pull-up for sequencing):
        # the unknown VIN must not be solved to a mid voltage through R1 / R3
        comps = [_comp("U1", "TPS62933"), _comp("R1", "1430k"), _comp("R2", "100k"),
                 _comp("R3", "100k"), _comp("L1", "10u"), _comp("C1", "10u")]
        nets = [_net(1, "VIN", ("U1", "1", "VIN", "power_in"), ("R1", "1", "", "passive")),
                _net(2, "EN", ("U1", "2", "EN", "input"), ("R1", "2", "", "passive"),
                     ("R2", "1", "", "passive"), ("R3", "1", "", "passive")),
                _net(3, "+3V3", ("R3", "2", "", "passive")),
                _net(4, "SW", ("U1", "3", "SW", "output"), ("L1", "1", "", "passive")),
                _net(5, "OUT", ("L1", "2", "", "passive"), ("C1", "1", "", "passive")),
                _net(6, "GND", ("U1", "9", "GND", "power_in"), ("R2", "2", "", "passive"),
                     ("C1", "2", "", "passive"))]
        nl = _nl(tmp_path, comps, nets)
        assert any(r["ref"] == "U1" for r in pr.detect_regulators(nl))
        volts = dc_model.solve(nl, {"+3V3": (3.3, "c")}, {})["volts"]
        assert "VIN" not in volts and "EN" not in volts

    def test_ldo_behind_a_filter_resistor_is_solved(self, tmp_path):
        # a regulator input fed from a known rail through <= 100 ohm keeps its voltage
        comps = [_comp("U1", "LDO"), _comp("R1", "10"), _comp("R2", "100k")]
        nets = [_net(1, "+5V", ("R1", "1", "", "passive")),
                _net(2, "VIN", ("R1", "2", "", "passive"), ("U1", "1", "VIN", "power_in"),
                     ("R2", "1", "", "passive")),
                _net(3, "VOUT", ("U1", "5", "VOUT", "power_out")),
                _net(4, "GND", ("U1", "2", "GND", "power_in"), ("R2", "2", "", "passive"))]
        nl = _nl(tmp_path, comps, nets)
        volts = dc_model.solve(nl, {"+5V": (5.0, "c")}, {})["volts"]
        assert volts["VIN"][0] == pytest.approx(5 * 100e3 / 100010, rel=1e-6)

    def test_led_string_stops_at_unknown_rail(self, tmp_path):
        nl = self._board(tmp_path)
        specs = {"D3": {"mpn": "L", "category": "led",
                        "electrical": {"VF": {"typ": 1.9, "unit": "V", "page": 1}}},
                 "D8": {"mpn": "C", "category": "current_regulator",
                        "electrical": {"I_reg": {"typ": 0.01, "unit": "A", "page": 1},
                                       "V_min": {"max": 1.8, "unit": "V", "page": 1}}}}
        f: list = []
        rows = part_ratings.check_leds(nl, specs, {}, f, lambda *a: None)
        assert "known voltage" in rows[0]["status"] and not f      # not across D6 to GND
        rows = part_ratings.check_leds(nl, specs, {"VBUS": 20.0}, f, lambda *a: None)
        assert rows[0]["top"] == "VBUS" and rows[0]["v_ccr"] == pytest.approx(18.1)


def test_all_passive_vendor_symbol_gets_supply_pins(tmp_path):
    comps = [_comp("IC5", "M95080"), _comp("U2", "TYPED"), _comp("C1", "100n")]
    nets = [_net(1, "+3V3", ("IC5", "8", "VCC", "passive"), ("U2", "1", "VCC", "passive"),
                 ("C1", "1", "", "passive")),
            _net(2, "GND", ("IC5", "4", "VSS", "passive"), ("U2", "2", "GND", "power_in"),
                 ("C1", "2", "", "passive")),
            _net(3, "SCK", ("IC5", "6", "C", "passive"), ("U2", "3", "SCK", "output"))]
    nl = _nl(tmp_path, comps, nets)
    types = {(p.ref, p.name): p.type for ps in nl.pins.values() for p in ps}
    assert types[("IC5", "VCC")] == types[("IC5", "VSS")] == "power_in"
    assert types[("IC5", "C")] == "passive" and nl.components["IC5"]["pin_types"] == "inferred"
    assert types[("U2", "VCC")] == "passive"          # a typed symbol is left alone


def test_inference_leaves_converter_inputs_alone(tmp_path):
    # OP-20 #4: an all-passive ADC: VINP / VINN are signal inputs, only VDD / GND are supplies;
    # a part with no ground-named pin is not touched at all
    comps = [_comp("U3", "ADC"), _comp("U4", "SENSOR")]
    nets = [_net(1, "AIN_P", ("U3", "1", "VINP", "passive")),
            _net(2, "AIN_N", ("U3", "2", "VINN", "passive")),
            _net(3, "+3V3", ("U3", "3", "VDD", "passive"), ("U4", "1", "VCC", "passive")),
            _net(4, "GND", ("U3", "4", "GND", "passive")),
            _net(5, "OUT", ("U4", "2", "OUT", "passive")),
            _net(6, "VB_DET", ("U3", "5", "VBUS", "passive"))]
    nl = _nl(tmp_path, comps, nets)
    types = {(p.ref, p.name): p.type for ps in nl.pins.values() for p in ps}
    assert types[("U3", "VDD")] == types[("U3", "GND")] == "power_in"
    assert types[("U3", "VBUS")] == "passive"                 # a sense pin on USB bridges
    assert types[("U3", "VINP")] == types[("U3", "VINN")] == "passive"
    assert types[("U4", "VCC")] == "passive" and "pin_types" not in nl.components["U4"]


def test_led_walk_stops_at_a_branch_and_names_a_loop(tmp_path):
    # OP-20 #46: the node above the LED also feeds R2 to another net: a branch
    comps = [_comp("D1", "RED", lib="Device:LED"), _comp("R1", "1k"), _comp("R2", "1k"),
             _comp("R3", "10k")]
    nets = [_net(1, "+5V", ("R1", "1", "", "passive")),
            _net(2, "A", ("R1", "2", "", "passive"), ("R2", "1", "", "passive"),
                 ("R3", "1", "", "passive")),
            _net(3, "B", ("R3", "2", "", "passive"), ("D1", "2", "A", "passive")),
            _net(4, "X", ("R2", "2", "", "passive")),
            _net(5, "GND", ("D1", "1", "K", "passive"))]
    specs = {"D1": {"mpn": "L", "category": "led",
                    "electrical": {"VF": {"typ": 1.9, "unit": "V", "page": 1}}}}
    f: list = []
    rows = part_ratings.check_leds(_nl(tmp_path, comps, nets), specs, {"+5V": 5.0}, f,
                                   lambda *a: None)
    assert "branch" in rows[0]["status"] and not f
    # OP-20 #47: both ends on one net
    comps = [_comp("D1", "RED", lib="Device:LED"), _comp("R1", "1k")]
    nets = [_net(1, "GND", ("R1", "1", "", "passive"), ("D1", "1", "K", "passive")),
            _net(2, "A", ("R1", "2", "", "passive"), ("D1", "2", "A", "passive"))]
    rows = part_ratings.check_leds(_nl(tmp_path, comps, nets, name="l.net"), specs, {"GND": 0.0},
                                   f, lambda *a: None)
    assert rows[0]["status"] == "string returns to GND: no supply across it"
