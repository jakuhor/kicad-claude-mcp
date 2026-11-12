"""Part ratings (OP-6), circuit detectors (OP-4), BOM lifecycle
(OP-5)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_claude.adapters import bom_tools, circuit_review, part_ratings, sch_io
from kicad_claude.adapters import datasheet_specs as dss
from kicad_claude.adapters import power_review as pr

FIXTURES = Path(__file__).parent / "fixtures"


def _comp(ref, value, fields=None, dnp=False, lib="Device:X", desc="", no_bom=False):
    f = " ".join(f'(field (name "{k}") "{v}")' for k, v in (fields or {}).items())
    props = (' (property (name "dnp"))' if dnp else "") + \
        (' (property (name "exclude_from_bom"))' if no_bom else "")
    a, b = lib.split(":")
    return (f'(comp (ref "{ref}") (value "{value}") (footprint "Lib:{value}_FP") '
            f'(libsource (lib "{a}") (part "{b}") (description "{desc}")) (fields {f}){props})')


def _net(code, name, *nodes):
    body = " ".join(f'(node (ref "{r}") (pin "{n}") (pinfunction "{fn}") (pintype "{t}"))'
                    if fn else f'(node (ref "{r}") (pin "{n}") (pintype "{t}"))'
                    for r, n, fn, t in nodes)
    return f'(net (code "{code}") (name "{name}") {body})'


def _nl(tmp_path, comps, nets, name="s.net"):
    p = tmp_path / name
    p.write_text(f'(export (version "E") (components {" ".join(comps)}) (nets {" ".join(nets)}))',
                 encoding="utf-8")
    return pr.parse_netlist(p)


def _sv(**kw):
    return {"unit": kw.pop("unit"), "page": 1, **kw}


# ---- datasheet spec curves / ratings ---------------------------------------- #


class TestSpecCurves:
    def test_curve_validation_and_interpolation(self):
        rec = {"category": "capacitor",
               "curves": {"C_bias": {"x_unit": "V", "y_unit": "%", "page": 1,
                                     "points": [[0, 100], [10, 80], [20, 50]]}}}
        errs, _ = dss.validate(rec, ["page one"])
        assert errs == []
        assert dss.c_bias_ratio(rec, 15) == pytest.approx(0.65)
        assert dss.c_bias_ratio(rec, -5) == pytest.approx(0.9)    # magnitude of bias
        assert dss.c_bias_ratio(rec, 99) == pytest.approx(0.5)    # clamped
        bad = {"category": "capacitor",
               "curves": {"C_bias": {"x_unit": "V", "y_unit": "%", "page": 1,
                                     "points": [[10, 80], [0, 100]]}}}
        assert any("ascending" in e for e in dss.validate(bad, ["p"])[0])

    def test_rated_magnitude_and_lowest_irms(self):
        rec = {"electrical": {"ID": _sv(max=-6, unit="A"),
                              "Irms_40C": _sv(typ=5.5, unit="A"), "Irms_20C": _sv(typ=4, unit="A")}}
        assert dss.rated(rec, "electrical", "ID")[0] == 6
        assert dss.rated(rec, "electrical", "Irms")[:1] == (4,)


# ---- part ratings ---------------------------------------------------------- #


def _buck(tmp_path, shunt=False):
    """J1 -> VIN -> buck U1 -> +5V; `shunt`: J1 feeds VIN through R9 0.1 Ohm."""
    comps = [_comp("J1", "PWR"), _comp("U1", "BUCK"), _comp("L1", "10u"),
             _comp("R1", "52.5k"), _comp("R2", "10k"), _comp("C1", "10u"), _comp("C2", "22u"),
             _comp("D1", "RED", lib="Device:LED"), _comp("R5", "1k")]
    jn = "VRAW" if shunt else "VIN"
    entry = [_net(1, jn, ("J1", "1", "VIN", "passive"), ("J1", "2", "VIN", "passive"),
                  *([("R9", "1", "", "passive")] if shunt else []))]
    if shunt:
        comps.append(_comp("R9", "0.1"))
        entry.append(_net(7, "VIN", ("R9", "2", "", "passive"), ("U1", "1", "VIN", "power_in"),
                          ("C1", "1", "", "passive")))
    else:
        entry[0] = _net(1, "VIN", ("J1", "1", "VIN", "passive"), ("J1", "2", "VIN", "passive"),
                        ("U1", "1", "VIN", "power_in"), ("C1", "1", "", "passive"))
    nets = [
        *entry,
        _net(2, "GND", ("J1", "3", "GND", "passive"), ("U1", "9", "GND", "power_in"),
             ("R2", "2", "", "passive"), ("C1", "2", "", "passive"), ("C2", "2", "", "passive"),
             ("D1", "1", "K", "passive")),
        _net(3, "SW", ("U1", "2", "SW", "output"), ("L1", "1", "", "passive")),
        _net(4, "+5V", ("L1", "2", "", "passive"), ("R1", "1", "", "passive"),
             ("C2", "1", "", "passive"), ("R5", "1", "", "passive")),
        _net(5, "FB", ("U1", "3", "FB", "input"), ("R1", "2", "", "passive"),
             ("R2", "1", "", "passive")),
        _net(6, "LEDA", ("R5", "2", "", "passive"), ("D1", "2", "A", "passive")),
    ]
    return _nl(tmp_path, comps, nets)


class TestPartRatings:
    def _run(self, tmp_path, loads, specs, **kw):
        nl = _buck(tmp_path)
        pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VIN": 12})
        return part_ratings.check(nl, pt, specs, loads, **kw)

    def test_current_model_and_ratings(self, tmp_path):
        specs = {
            "U1": {"mpn": "BUCK1", "thermal": {"rth_ja": _sv(typ=50, unit="C/W"),
                                               "tj_max": _sv(max=125, unit="C")},
                   "recommended": {"IOUT": _sv(max=2, unit="A")},
                   "regulator": {"fsw": _sv(typ=500e3, unit="Hz")}},
            "L1": {"mpn": "IND1", "electrical": {"Isat": _sv(typ=3.2, unit="A"),
                                                 "Irms_40C": _sv(typ=4, unit="A")}},
            "J1": {"mpn": "CONN1", "electrical": {"I_rated_total": _sv(max=1, unit="A")}},
            "D1": {"mpn": "LED1", "category": "led", "electrical": {"VF": _sv(typ=2, unit="V")},
                   "absolute_max": {"IF": _sv(max=0.002, unit="A")}},
        }
        r = self._run(tmp_path, {"+5V": 3.0}, specs, ambient_c=25)
        rules = {(f["rule"], f["ref"]): f["severity"] for f in r["findings"]}
        u1 = r["regulators"]["U1"]
        assert u1["vout"] == pytest.approx(5.0, rel=1e-3)
        # Iin = 5 V * 3 A / (0.85 * 12 V)
        assert u1["iin"] == pytest.approx(15 / (0.85 * 12), rel=1e-3)
        assert r["assumptions"]["efficiency_assumed_for"] == ["U1"]
        # buck ripple (12-5)*5 / (12 * 10u * 500k) = 0.583 A -> peak 3.29 A > Isat 3.2
        assert r["inductors"]["L1"]["ripple_pp"] == pytest.approx(0.5833, rel=1e-3)
        assert rules[("RT-L-ISAT", "L1")] == "error"
        assert rules[("RT-REG-IOUT", "U1")] == "warning"       # 3 A > recommended 2 A
        assert rules[("RT-CONN-I", "J1")] == "error"           # 1.47 A > 1 A collective
        assert rules[("RT-TJ", "U1")] == "error"               # 25 + 50 * 2.65 W > 125
        led = r["leds"][0]
        assert led["current"] == pytest.approx(0.003)          # (5 - 2) / 1k
        assert rules[("RT-LED", "D1")] == "error"              # > 2 mA

    def test_efficiency_and_no_data(self, tmp_path):
        r = self._run(tmp_path, {"+5V": 0.5}, {}, efficiency={"U1": 0.9})
        assert r["regulators"]["U1"]["eta_source"] == "caller"
        assert r["assumptions"]["ambient_source"].startswith("default")
        assert any(f["rule"] == "RT-NODATA" and f["ref"] == "L1" for f in r["findings"])
        path = r["input_paths"]["VIN"]
        assert path["parts"] == []          # VIN is the connector net itself


    def _tj(self, tmp_path, specs, footprint):
        nl = _buck(tmp_path)
        nl.components["U1"]["footprint"] = footprint
        pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VIN": 12})
        r = part_ratings.check(nl, pt, specs, {"+5V": 3.0}, ambient_c=25)
        tj = [f for f in r["findings"] if f["rule"] == "RT-TJ" and f["ref"] == "U1"]
        row = next(x for x in r["thermal"] if x["ref"] == "U1")
        return tj, row, r

    def test_tj_package_table(self, tmp_path):
        tjmax = {"tj_max": _sv(max=125, unit="C")}
        # no datasheet rth_ja: QFN 3x3 table value 50 C/W, error capped at warning
        tj, row, _ = self._tj(tmp_path, {"U1": {"mpn": "BUCK1", "thermal": tjmax}},
                              "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm")
        assert row["rth_ja"] == 50 and row["rth_source"] == "package_typical (assumed)"
        assert [f["severity"] for f in tj] == ["warning"]
        assert "package-typical" in tj[0]["message"]
        # datasheet value wins over the table (10 C/W: 25 + 26.5 C, far below 125)
        tj, row, _ = self._tj(tmp_path, {"U1": {"mpn": "BUCK1", "thermal": {
            **tjmax, "rth_ja": _sv(typ=10, unit="C/W")}}}, "Package_DFN_QFN:QFN-16-1EP_3x3mm")
        assert row["rth_ja"] == 10 and row["rth_source"] == "datasheet BUCK1 p.1" and tj == []
        # unknown package: no estimate, RT-NODATA as before
        tj, row, r = self._tj(tmp_path, {}, "Lib:CustomThing")
        assert "rth_ja" not in row and tj == []
        assert any(f["rule"] == "RT-NODATA" and f["ref"] == "U1" for f in r["findings"])

    def test_dissipation(self, tmp_path):
        specs = {"U1": {"mpn": "BUCK1", "thermal": {"rth_ja": _sv(typ=50, unit="C/W"),
                                                    "tj_max": _sv(max=125, unit="C")}}}
        r = self._run(tmp_path, {"+5V": 3.0}, specs, ambient_c=25)
        d = {x["ref"]: x for x in r["dissipation"]}
        # divider R1 52.5k / R2 10k on +5 V: FB at 0.8 V from the DC model
        assert d["R1"]["p_w"] == pytest.approx(4.2 ** 2 / 52.5e3, rel=1e-3)
        assert d["R2"]["p_w"] == pytest.approx(0.8 ** 2 / 10e3, rel=1e-3)
        assert d["R1"]["method"].startswith("DC model")
        # regulator unchanged: Pout (1 - eta) / eta = 15 * 0.15 / 0.85
        assert d["U1"]["p_w"] == pytest.approx(15 * 0.15 / 0.85, rel=1e-3)
        assert r["dissipation"][0]["ref"] == "U1"          # sorted by power

    def test_shunt_dissipation(self, tmp_path):
        nl = _buck(tmp_path, shunt=True)
        pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VIN": 12})
        r = part_ratings.check(nl, pt, {}, {"+5V": 3.0})
        iin = 15 / (0.85 * 12)
        d = {x["ref"]: x for x in r["dissipation"]}
        assert d["R9"]["p_w"] == pytest.approx(iin ** 2 * 0.1, rel=1e-3)
        assert d["R9"]["method"].startswith("I^2 R")

    def test_tj_margin_follows_profile(self, tmp_path):
        specs = {"U1": {"mpn": "BUCK1", "thermal": {"rth_ja": _sv(typ=50, unit="C/W"),
                                                    "tj_max": _sv(max=125, unit="C")}}}
        # P = 5 V * 1.47 A * 0.15 / 0.85 = 1.30 W -> Tj 25 + 65 = 90 C
        com = self._run(tmp_path, {"+5V": 1.47}, specs, ambient_c=25)
        mil = self._run(tmp_path, {"+5V": 1.47}, specs, ambient_c=25, profile="military")
        assert not [f for f in com["findings"] if f["rule"] == "RT-TJ"]     # margin 25 C
        assert [f["severity"] for f in mil["findings"] if f["rule"] == "RT-TJ"] == ["warning"]
        assert mil["assumptions"]["tj_margin_c"] == 40

    def test_resistor_power(self, tmp_path):
        # shunt R9 0.1 Ohm at Iin 1.47 A: 0.216 W
        def run(power):
            nl = _buck(tmp_path, shunt=True)
            if power:
                nl.components["R9"]["fields"]["Power"] = power
            pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VIN": 12})
            r = part_ratings.check(nl, pt, {}, {"+5V": 3.0})
            return {(f["rule"], f["ref"]): f["severity"] for f in r["findings"]}
        assert run("1W").get(("RT-R-P", "R9")) is None
        assert run("1/4W")[("RT-R-P", "R9")] == "warning"      # 86 % > 50 %
        assert run("200mW")[("RT-R-P", "R9")] == "error"
        assert run("")[("RT-NODATA", "R9")] == "info"          # >= 0.1 W, no rating

    @pytest.mark.parametrize("fp, pkg, rth, ep", [
        ("Package_TO_SOT_SMD:SOT-23-5", "SOT-23", 250, False),
        ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "SOIC-8", 120, False),
        ("Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm", "QFN-16-1EP_3x3", 50, True),
        ("Package_TO_SOT_SMD:TO-252-2", "TO-252", 40, False),
        ("Package_DFN_QFN:DFN-8-1EP_2x2mm_P0.5mm", "DFN-8-1EP_2x2", 70, True),
        ("Lib:MyConnector", None, None, None),
    ])
    def test_package_classifier(self, fp, pkg, rth, ep):
        from kicad_claude.adapters import thermal_pkg
        got = thermal_pkg.classify(fp)
        if pkg is None:
            assert got is None
        else:
            assert (got["package"], got["rth_ja"], got["exposed_pad"]) == (pkg, rth, ep)


# ---- circuit detectors ---------------------------------------------------- #


def _circuits(tmp_path):
    comps = [
        _comp("J1", "USB_C", lib="Connector:USB_C_Receptacle"), _comp("R1", "5.1k"),
        _comp("R2", "5.1k"),
        _comp("U1", "OPA2", lib="Amplifier_Operational:OPA2"), _comp("C1", "100n"),
        _comp("R3", "10k"), _comp("C2", "1u"),
        _comp("U2", "MCU"), _comp("U3", "SENSOR"), _comp("U4", "FLASH"), _comp("R4", "470"),
        _comp("D1", "LED", lib="Device:LED"),
        _comp("U5", "OLED"), _comp("U6", "RTC"), _comp("J2", "AUDIO", lib="Connector:AudioJack3"),
    ]
    nets = [
        _net(1, "VBUS", ("J1", "A4", "VBUS", "passive"), ("J1", "B4", "VBUS", "passive")),
        _net(2, "GND", ("J1", "A1", "GND", "passive"), ("R1", "2", "", "passive"),
             ("U1", "4", "V-", "power_in"), ("R3", "2", "", "passive"),
             ("D1", "1", "K", "passive"), ("U2", "20", "VSS", "power_in"),
             ("U3", "20", "GND", "power_in"), ("U4", "20", "GND", "power_in")),
        _net(3, "CC1", ("J1", "A5", "CC1", "bidirectional"), ("R1", "1", "", "passive"),
             ("R2", "1", "", "passive")),
        _net(4, "CC2", ("J1", "B5", "CC2", "bidirectional"), ("R2", "2", "", "passive")),
        # op-amp channel A: +IN only through C1 (no DC path); channel B unused
        _net(5, "SIG_IN", ("J2", "T", "T", "passive"), ("C1", "1", "", "passive")),
        _net(6, "AC_IN", ("C1", "2", "", "passive"), ("U1", "3", "+INA", "input")),
        _net(7, "OUTA", ("U1", "1", "OUTA", "output"), ("U1", "2", "-INA", "input"),
             ("C2", "1", "", "passive")),
        _net(8, "unconnected-(U1-+INB-Pad5)", ("U1", "5", "+INB", "input")),
        _net(9, "OUTB", ("U1", "7", "OUTB", "output"), ("U1", "6", "-INB", "input")),
        # high-pass: C2 into R3 at U2 input
        _net(10, "HP", ("C2", "2", "", "passive"), ("R3", "1", "", "passive"),
             ("U2", "1", "AIN", "input")),
        _net(11, "+3V3", ("U2", "21", "VDD", "power_in"), ("U3", "21", "VDD", "power_in"),
             ("U4", "21", "VCC", "power_in"), ("R4", "1", "", "passive"),
             ("D1", "2", "A", "passive"), ("U5", "21", "VCC", "power_in")),
        _net(12, "+5V", ("U6", "21", "VCC", "power_in")),
        _net(13, "SDA", ("U2", "2", "SDA", "bidirectional"), ("U3", "2", "SDA", "bidirectional"),
             ("R4", "2", "", "passive")),
        _net(14, "CS", ("U2", "3", "CS0", "output"), ("U3", "3", "CS", "input"),
             ("U4", "3", "CS", "input")),
        _net(15, "SCK", ("U2", "4", "SCK", "output"), ("U3", "4", "SCK", "input"),
             ("U4", "4", "SCK", "input")),
        _net(16, "FLOAT", ("U3", "5", "INT_EN", "input"), ("U4", "5", "WP", "input")),
        _net(17, "LVL", ("U6", "5", "OUT", "output"), ("U2", "5", "PA1", "input")),
        _net(18, "VCOMH", ("U5", "1", "VCOMH", "passive")),
        _net(19, "C1P", ("U5", "2", "C1P", "passive")),
        _net(20, "C1N", ("U5", "3", "C1N", "passive")),
        _net(21, "DC", ("U5", "4", "D/C", "input"), ("U2", "6", "PA2", "output")),
        _net(22, "unconnected-(U6-VBAT-Pad3)", ("U6", "3", "VBAT", "power_in")),
    ]
    return _nl(tmp_path, comps, nets)


def _entry(tmp_path, prot):
    """Barrel jack J1 -> fuse F1 -> [protection] -> VIN -> buck U1 -> +5V."""
    comps = [_comp("J1", "BARREL", lib="Connector:Barrel_Jack"), _comp("F1", "PTC"),
             _comp("U1", "BUCK"), _comp("L1", "10u"), _comp("R1", "52.5k"), _comp("R2", "10k")]
    gnd = [("J1", "2", "GND", "passive"), ("U1", "9", "GND", "power_in"),
           ("R2", "2", "", "passive")]
    nets = [_net(1, "VJ", ("J1", "1", "VIN", "passive"), ("F1", "1", "", "passive"))]
    if prot == "diode":
        comps.append(_comp("D1", "SS34", lib="Device:D_Schottky"))
        nets += [_net(2, "VF", ("F1", "2", "", "passive"), ("D1", "2", "A", "passive")),
                 _net(3, "VIN", ("D1", "1", "K", "passive"), ("U1", "1", "VIN", "power_in"))]
    elif prot in ("pfet", "pfet_reversed"):
        comps.append(_comp("Q1", "PMOS", lib="Device:Q_PMOS_GSD"))
        d, s_ = ("VF", "VIN") if prot == "pfet" else ("VIN", "VF")
        nets += [_net(2, "VF", ("F1", "2", "", "passive"),
                      ("Q1", "3" if d == "VF" else "2", "D" if d == "VF" else "S", "passive")),
                 _net(3, "VIN", ("Q1", "2" if d == "VF" else "3", "S" if d == "VF" else "D",
                                 "passive"), ("U1", "1", "VIN", "power_in"))]
        gnd.append(("Q1", "1", "G", "input"))
    else:
        nets += [_net(3, "VIN", ("F1", "2", "", "passive"), ("U1", "1", "VIN", "power_in"))]
    nets += [_net(4, "GND", *gnd),
             _net(5, "SW", ("U1", "2", "SW", "output"), ("L1", "1", "", "passive")),
             _net(6, "+5V", ("L1", "2", "", "passive"), ("R1", "1", "", "passive")),
             _net(7, "FB", ("U1", "3", "FB", "input"), ("R1", "2", "", "passive"),
                  ("R2", "1", "", "passive"))]
    return _nl(tmp_path, comps, nets)


class TestProtection:
    @pytest.mark.parametrize("prot, flagged, kind", [
        ("none", True, None), ("diode", False, "series diode"),
        ("pfet", False, "series FET"), ("pfet_reversed", True, None),
    ])
    def test_reverse_polarity(self, tmp_path, prot, flagged, kind):
        nl = _entry(tmp_path, prot)
        pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VJ": 12, "VIN": 12})
        r = circuit_review.analyze(nl, pt, {}, ("power",))
        got = [f for f in r["findings"] if f["rule"] == "PW-REVPOL"]
        assert bool(got) is flagged
        row = r["reverse_polarity"][0]
        assert row["connector"] == "J1" and "U1" in row["loads"] or not flagged
        if kind:
            assert row["protection"]["type"].startswith(kind)

    def test_usb_skipped(self, tmp_path):
        nl = _entry(tmp_path, "none")
        nl.components["J1"]["lib_id"] = "Connector:USB_B_Micro"
        pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VJ": 12, "VIN": 12})
        r = circuit_review.analyze(nl, pt, {}, ("power",))
        assert not [f for f in r["findings"] if f["rule"] == "PW-REVPOL"]

    @pytest.mark.parametrize("spec, field, sev", [
        ({"I_hold": _sv(min=0.5, unit="A")}, "", "error"),          # 0.5 A < 1.47 A load
        ({"I_hold": _sv(min=2.0, unit="A"), "I_trip": _sv(max=1.2, unit="A")}, "", "error"),
        ({"I_hold": _sv(min=2.0, unit="A")}, "", None),
        ({"I_hold": _sv(min=10.0, unit="A")}, "", None),            # > 5x: RT-OVERSPEC
        ({}, "1A", "error"),                                        # Current field
        ({}, "", "nodata"),
    ])
    def test_fuse_sizing(self, tmp_path, spec, field, sev):
        nl = _entry(tmp_path, "none")
        if field:
            nl.components["F1"]["fields"]["Current"] = field
        specs = {"F1": {"mpn": "PTC1", "category": "fuse", "electrical": spec}} if spec else {}
        pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VJ": 12, "VIN": 12})
        r = part_ratings.check(nl, pt, specs, {"+5V": 3.0})
        got = {f["severity"] for f in r["findings"] if f["rule"] == "RT-FUSE"}
        if sev == "nodata":
            assert any(f["rule"] == "RT-NODATA" and f["ref"] == "F1" for f in r["findings"])
        elif sev is None:
            assert not got
        else:
            assert got == {sev}


class TestCircuitReview:
    def test_detectors(self, tmp_path):
        nl = _circuits(tmp_path)
        pt = pr.analyze(nl, rail_voltages={"+3V3": 3.3, "+5V": 5.0})
        r = circuit_review.analyze(nl, pt, {})
        got = {(f["rule"], f["ref"]) for f in r["findings"]}
        assert ("PW-USBC-CC", "R2") in got          # one resistor joins CC1 and CC2
        assert ("PW-VBUS-TVS", "J1") in got
        assert ("AU-BIAS", "U1") in got             # +INA only through C1
        assert ("AU-UNUSED", "U1") in got           # channel B +IN unconnected
        assert ("DG-I2C-RPU", "R4") in got          # 470 R to 3.3 V: 6.2 mA
        assert ("DG-SPI-CS", "U3") in got           # CS shared by U3 and U4
        assert ("DG-FLOAT", "U3") in got            # INT_EN / WP: inputs only
        assert ("DG-LEVEL", "U2") in got            # 5 V output into a 3.3 V input
        assert ("LE-NORES", "D1") in got            # LED across 3.3 V
        assert ("DS-CAP", "U5") in got              # VCOMH / C1P-C1N without caps
        assert ("RTC-VBAT", "U6") in got
        hp = r["opamps"]
        assert hp and hp[0]["ref"] == "U1"

    def test_highpass_corner(self, tmp_path):
        comps = [_comp("U1", "OPA"), _comp("C1", "100n"), _comp("R1", "10k"), _comp("J1", "IN")]
        nets = [_net(1, "IN", ("J1", "1", "", "passive"), ("C1", "1", "", "passive")),
                _net(2, "P", ("C1", "2", "", "passive"), ("R1", "1", "", "passive"),
                     ("U1", "3", "+IN", "input")),
                _net(3, "GND", ("R1", "2", "", "passive"), ("J1", "2", "", "passive")),
                _net(4, "O", ("U1", "1", "OUT", "output"), ("U1", "2", "-IN", "input"))]
        nl = _nl(tmp_path, comps, nets)
        r = circuit_review.analyze(nl, pr.analyze(nl), {}, ("analog",))
        hpf = [f for f in r["findings"] if f["rule"] == "AU-HPF"]
        assert hpf and "159" in hpf[0]["message"]      # 1 / (2 pi 10k 100n)
        assert not [f for f in r["findings"] if f["rule"] == "AU-BIAS"]

    def test_anyout_pins_may_float(self, tmp_path):
        comps = [_comp("U1", "LDO")]
        nets = [_net(1, "unconnected-(U1-0.1V-Pad12)", ("U1", "12", "0.1V", "input")),
                _net(2, "unconnected-(U1-X-Pad3)", ("U1", "3", "X", "input"))]
        nl = _nl(tmp_path, comps, nets)
        specs = {"U1": {"mpn": "LDO1", "regulator": {"anyout": {"pins": {"12": {}}}}}}
        r = circuit_review.analyze(nl, pr.analyze(nl), specs, ("digital",))
        floats = [f for f in r["findings"] if f["rule"] == "DG-FLOAT"]
        assert len(floats) == 1 and "X" in floats[0]["message"]


# ---- BOM lifecycle --------------------------------------------------------- #


def _bom_nl(tmp_path, name, r2="10k", extra=False):
    comps = [_comp("R1", "10k", {"MPN": "RC10K", "LCSC": "C1"}),
             _comp("R2", r2, {"MPN": "RC" + r2.upper(), "LCSC": "C2"}),
             _comp("C1", "100n", {"MPN": "CAP100N"}),
             _comp("TP1", "TP"), _comp("U1", "MCU", {"MPN": "MCU1", "LCSC": "C9"}, no_bom=extra)]
    if extra:
        comps.append(_comp("R3", "10k", {"MPN": "RC10K", "LCSC": "C1"}))
    return _nl(tmp_path, comps, [], name)


class TestBomTools:
    def test_diff(self, tmp_path):
        a = _bom_nl(tmp_path, "a.net")
        b = _bom_nl(tmp_path, "b.net", r2="22k", extra=True)
        d = bom_tools.diff_bom(a, b)
        assert [x["ref"] for x in d["added"]] == ["R3"]
        ch = {c["ref"]: c for c in d["changed"]}
        assert ch["R2"]["value"] == ["10k", "22k"] and ch["U1"]["in_bom"] == [True, False]
        q = {x["line"]: x for x in d["line_qty_changes"]}
        assert "RC10K" not in q                     # R2 left the line, R3 joined it
        assert q["RC22K"]["qty_new"] == 1 and q["MCU1"]["qty_new"] == 0
        assert d["mpns_new"] == ["RC22K"] and d["mpns_gone"] == ["MCU1"]

    def test_jlc_bom_and_cpl(self, tmp_path):
        nl = _bom_nl(tmp_path, "a.net", extra=True)
        bom, f = bom_tools.fab_bom(nl, "jlcpcb")
        assert bom[0] == ["Comment", "Designator", "Footprint", "LCSC Part #"]
        rows = {r[1]: r for r in bom[1:]}
        assert rows["R1,R3"][3] == "C1" and "U1" not in rows   # excluded from BOM
        assert any(x["rule"] == "FB-NO-LCSC" and "C1" in x["ref"] for x in f)
        pos = {"R1": {"x": 1.0, "y": -2.0, "rot": -90.0, "side": "top"},
               "R3": {"x": 3.0, "y": -2.0, "rot": 0.0, "side": "bottom"},
               "C1": {"x": 5.0, "y": -2.0, "rot": 0.0, "side": "top"}}
        cpl, f2 = bom_tools.fab_cpl(nl, pos, "jlcpcb", {"^10k": 180})
        r = {x[0]: x for x in cpl[1:]}
        assert r["R1"] == ["R1", "1.0000mm", "-2.0000mm", "Top", "90"]   # -90 + 180
        assert r["R3"][3] == "Bottom" and r["R3"][4] == "180"
        assert any(x["rule"] == "FB-NO-POS" and x["ref"] == "R2" for x in f2)

    def test_pcbway_bom(self, tmp_path):
        nl = _bom_nl(tmp_path, "a.net")
        bom, f = bom_tools.fab_bom(nl, "pcbway", smd_refs={"R1", "R2", "C1"})
        assert bom[0][4] == "Mfg Part #"
        mcu = next(r for r in bom[1:] if r[1] == "U1")
        assert mcu[7] == "THT" and mcu[2] == "1"
        assert any(x["rule"] == "FB-NO-MFR" for x in f)

    def test_csv_updates(self):
        u = bom_tools.parse_updates_csv("Designator,MPN,Manufacturer\n\"R1, R2\",X1,\nC1,,Murata\n")
        assert u == {"R1": {"MPN": "X1"}, "R2": {"MPN": "X1"}, "C1": {"Manufacturer": "Murata"}}

    def test_write_fields(self, tmp_path):
        sch = tmp_path / "t.kicad_sch"
        shutil.copy(FIXTURES / "kicad10_ecc83-pp_v2.kicad_sch", sch)
        dry = bom_tools.write_fields(sch, {"R1": {"MPN": "ABC"}, "R99": {"MPN": "x"}},
                                     dry_run=True)
        assert dry["change_count"] == 1 and dry["not_found"] == ["R99"]
        assert "ABC" not in sch.read_text(encoding="utf-8")
        done = bom_tools.write_fields(sch, {"R1": {"MPN": "ABC"}}, dry_run=False,
                                      save_all=lambda pairs: [sch_io.write_file(p, t) for p, t in pairs])
        assert done["change_count"] == 1
        again = bom_tools.write_fields(sch, {"R1": {"MPN": "ABC"}}, dry_run=True)
        assert again["change_count"] == 0
        with pytest.raises(ValueError):
            bom_tools.write_fields(sch, {"R1": {"Reference": "R5"}})


class TestI2cRise:
    def _bus(self, tmp_path):
        comps = [_comp("U1", "MCU"), _comp("U2", "SENSOR"), _comp("R1", "4.7k"),
                 _comp("R2", "4.7k")]
        nets = [_net(1, "+3V3", ("U1", "1", "VDD", "power_in"), ("U2", "1", "VDD", "power_in"),
                     ("R1", "1", "", "passive"), ("R2", "1", "", "passive")),
                _net(2, "SDA", ("U1", "2", "SDA", "bidirectional"),
                     ("U2", "2", "SDA", "bidirectional"), ("R1", "2", "", "passive")),
                _net(3, "SCL", ("U1", "3", "SCL", "output"), ("U2", "3", "SCL", "input"),
                     ("R2", "2", "", "passive")),
                _net(4, "GND", ("U1", "4", "GND", "power_in"), ("U2", "4", "GND", "power_in"))]
        return _nl(tmp_path, comps, nets)

    def _rise(self, tmp_path, specs=None, **kw):
        nl = self._bus(tmp_path)
        pt = pr.analyze(nl, rail_voltages={"+3V3": 3.3})
        r = circuit_review.analyze(nl, pt, specs or {}, ("digital",), **kw)
        return {f["ref"]: f["severity"] for f in r["findings"] if f["rule"] == "DG-I2C-RISE"}, r

    def test_rise_time_modes(self, tmp_path):
        caps = {"SDA": 200, "SCL": 200}
        # 0.8473 * 4.7k * 200 pF = 796 ns
        fast, r = self._rise(tmp_path, i2c_bus_capacitance_pf=caps)
        assert fast == {"SDA": "warning", "SCL": "warning"}
        row = next(x for x in r["i2c_pullups"] if x.get("line") == "SDA")
        assert row["tr_ns"] == pytest.approx(796.5, abs=1)
        std, _ = self._rise(tmp_path, i2c_mode="standard", i2c_bus_capacitance_pf=caps)
        assert std == {}

    def test_rise_time_from_specs_and_missing(self, tmp_path):
        missing, _ = self._rise(tmp_path)
        assert missing == {"SDA": "info", "SCL": "info"}
        cin = {"electrical": {"C_in": _sv(max=10e-12, unit="F")}}
        specs = {"U1": {"mpn": "MCU1", **cin}, "U2": {"mpn": "SEN1", **cin}}
        ok, r = self._rise(tmp_path, specs)
        assert ok == {}                  # 0.8473 * 4.7k * 20 pF = 80 ns
        row = next(x for x in r["i2c_pullups"] if x.get("line") == "SCL")
        assert row["c_bus_pf"] == pytest.approx(20)


class TestUartLevels:
    def _uart(self, tmp_path, series_r):
        """5 V MCU U1 TX -> 3.3 V module U2 RX, directly or through R1."""
        comps = [_comp("U1", "MCU5V"), _comp("U2", "BLE_MODULE")]
        tx = [("U1", "2", "TXD", "output")]
        rx = [("U2", "2", "RXD", "input")]
        nets = [_net(1, "+5V", ("U1", "1", "VCC", "power_in")),
                _net(2, "+3V3", ("U2", "1", "VDD", "power_in")),
                _net(5, "GND", ("U1", "4", "GND", "power_in"), ("U2", "4", "GND", "power_in"))]
        if series_r:
            comps.append(_comp("R1", "1k"))
            nets += [_net(3, "MCU_TX", *tx, ("R1", "1", "", "passive")),
                     _net(4, "MOD_RX", *rx, ("R1", "2", "", "passive"))]
        else:
            nets.append(_net(3, "UART_TX", *tx, *rx))
        nl = _nl(tmp_path, comps, nets)
        pt = pr.analyze(nl, rail_voltages={"+5V": 5.0, "+3V3": 3.3})
        r = circuit_review.analyze(nl, pt, {}, ("digital",))
        return {(f["rule"], f["ref"]) for f in r["findings"]}, r

    def test_direct(self, tmp_path):
        got, _ = self._uart(tmp_path, False)
        assert ("DG-LEVEL", "U2") in got

    def test_through_series_resistor(self, tmp_path):
        got, r = self._uart(tmp_path, True)
        assert ("DG-UART-LEVEL", "U2") in got
        row = r["uart_levels"][0]
        assert (row["tx"], row["rx"], row["via"]) == ("U1.TXD", "U2.RXD", ["R1"])


class TestInrush:
    def _usb(self, tmp_path, uf="100u"):
        comps = [_comp("J1", "USB_C", lib="Connector:USB_C_Receptacle"), _comp("C1", uf),
                 _comp("FB1", "600R"), _comp("C2", "1u"), _comp("U1", "LDO")]
        nets = [_net(1, "VBUS", ("J1", "A4", "VBUS", "passive"), ("C1", "1", "", "passive"),
                     ("FB1", "1", "", "passive")),
                _net(2, "VB_F", ("FB1", "2", "", "passive"), ("C2", "1", "", "passive"),
                     ("U1", "1", "VIN", "power_in")),
                _net(3, "GND", ("J1", "A1", "GND", "passive"), ("C1", "2", "", "passive"),
                     ("C2", "2", "", "passive"), ("U1", "2", "GND", "power_in"))]
        return _nl(tmp_path, comps, nets)

    def _run(self, nl, **kw):
        pt = pr.analyze(nl, rail_voltages={"VBUS": 5.0})
        r = circuit_review.analyze(nl, pt, {}, ("power",), **kw)
        return [f for f in r["findings"] if f["rule"] == "PW-INRUSH"], r

    def test_usb_inrush(self, tmp_path):
        got, r = self._run(self._usb(tmp_path), source_ohm=0.1, inrush_limit_a=5)
        row = r["inrush"]["entries"][0]
        assert row["capacitance_uf"] == pytest.approx(101)       # C1 + C2 through FB1
        assert row["peak_a"] == pytest.approx(50)                # 5 V / 0.1 Ohm
        msgs = " ".join(f["message"] for f in got)
        assert {f["severity"] for f in got} == {"warning"}
        assert "USB 2.0" in msgs and "inrush_limit_a" in msgs

    def test_unknown_source_is_info(self, tmp_path):
        got, _ = self._run(self._usb(tmp_path, "4.7u"))
        assert [f["severity"] for f in got] == ["info"]

    def test_soft_start(self, tmp_path):
        nl = _buck(tmp_path)
        specs = {"U1": {"mpn": "BUCK1", "regulator": {"t_ss": _sv(min=1e-3, unit="s")}}}
        pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VIN": 12})
        r = circuit_review.analyze(nl, pt, specs, ("power",), inrush_limit_a=0.1)
        row = r["inrush"]["regulator_outputs"][0]
        assert row["charge_current_a"] == pytest.approx(22e-6 * 5 / 1e-3, rel=1e-3)
        assert any(f["rule"] == "PW-INRUSH" and f["ref"] == "U1" for f in r["findings"])


class TestOverspec:
    def test_cases(self, tmp_path):
        nl = _buck(tmp_path)
        nl.components["C1"]["fields"]["Voltage"] = "100V"          # 12 V input cap
        nl.components["C2"]["fields"]["Voltage"] = "10V"           # 5 V output cap: fine
        specs = {"U1": {"mpn": "BUCK1", "recommended": {"IOUT": _sv(max=5, unit="A")}}}
        pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VIN": 12})
        r = part_ratings.check(nl, pt, specs, {"+5V": 0.3})
        got = {f["ref"] for f in r["findings"] if f["rule"] == "RT-OVERSPEC"}
        assert got == {"C1", "U1"}                                 # 100 V >= 4 x 12 V; 5 A >= 3 A
        assert all(f["severity"] == "info" for f in r["findings"] if f["rule"] == "RT-OVERSPEC")

    def test_fuse(self, tmp_path):
        nl = _entry(tmp_path, "none")
        specs = {"F1": {"mpn": "PTC1", "category": "fuse",
                        "electrical": {"I_hold": _sv(min=10.0, unit="A")}}}
        pt = pr.analyze(nl, vref={"U1": 0.8}, rail_voltages={"VJ": 12, "VIN": 12})
        r = part_ratings.check(nl, pt, specs, {"+5V": 3.0})
        assert ("RT-OVERSPEC", "F1") in {(f["rule"], f["ref"]) for f in r["findings"]}
