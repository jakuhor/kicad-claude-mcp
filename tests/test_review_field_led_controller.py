"""OP-18 J: review false positives found on the JH_LED_control field test.

A 24 V jack behind an input fuse, a buck whose output is OR-ed with USB VBUS
through two Schottkys into +5V, a fixed 3.3 V LDO, and a P-FET high-side
switch with a gate pull-up, a 12 V zener clamp and an N-FET pull-down.
"""

from __future__ import annotations

import pytest

from kicad_claude.adapters import circuit_review, connectivity_review, dc_model, part_ratings
from kicad_claude.adapters import power_review as pr

from .test_review_ratings_circuits_bom import _comp, _net, _nl


def _board(tmp_path):
    comps = [
        _comp("J1", "PJ-063AH", lib="Connector:Barrel_Jack_MountingPin", desc="DC Barrel Jack"),
        _comp("F1", "7A", lib="Device:Fuse"),
        _comp("U1", "BUCK"), _comp("L1", "10u"), _comp("R2", "107k"), _comp("R3", "24.9k"),
        _comp("C8", "22u"), _comp("C9", "22u"),
        _comp("D10", "1N5819", lib="Device:D_Schottky"),
        _comp("D9", "1N5819", lib="Device:D_Schottky"),
        _comp("J4", "USB_C", lib="Connector:USB_C_Receptacle"), _comp("C18", "1u"),
        _comp("U2", "TLV75733PDYDR", lib="Regulator_Linear:TLV75733PDBV",
              desc="1A Low IQ LDO, Fixed Output 3.3V, SOT-23-5"),
        _comp("C10", "1u"), _comp("C11", "22u"),
        _comp("Q2", "DMP4015", lib="Transistor_FET:Q_PMOS_SGD"),
        _comp("R5", "100k"), _comp("R6", "47k"),
        _comp("D3", "MMSZ5242B", lib="Device:D_Zener"),
        _comp("Q3", "2N7002", lib="Transistor_FET:2N7002", desc="0.115A Id, 60V Vds, N-Channel MOSFET"),
        _comp("R7", "4.7k"), _comp("R8", "100k"), _comp("U5", "MCU"),
        _comp("F2", "7A", lib="Device:Fuse"), _comp("J2", "LED", lib="Connector_Generic:Conn_01x03"),
    ]
    nets = [
        _net(1, "Net-(F1-Pad1)", ("J1", "1", "", "passive"), ("F1", "1", "", "passive")),
        _net(2, "VIN", ("F1", "2", "", "passive"), ("U1", "2", "VIN", "power_in"),
             ("Q2", "1", "S", "passive"), ("R5", "1", "", "passive"), ("D3", "1", "K", "passive")),
        _net(3, "GND", ("J1", "2", "", "passive"), ("U1", "1", "PGND", "power_in"),
             ("R3", "2", "", "passive"), ("C8", "2", "", "passive"), ("C9", "2", "", "passive"),
             ("J4", "A1", "GND", "passive"), ("C18", "2", "", "passive"),
             ("U2", "2", "GND", "power_in"), ("C10", "2", "", "passive"),
             ("C11", "2", "", "passive"), ("Q3", "2", "S", "passive"),
             ("R8", "2", "", "passive"), ("U5", "1", "GND", "power_in"),
             ("J2", "1", "", "passive")),
        _net(4, "SW", ("U1", "8", "SW", "output"), ("L1", "1", "", "passive")),
        _net(5, "5V_BUCK", ("L1", "2", "", "passive"), ("R2", "1", "", "passive"),
             ("C8", "1", "", "passive"), ("C9", "1", "", "passive"), ("D10", "2", "A", "passive")),
        _net(6, "FB", ("U1", "5", "FB", "input"), ("R2", "2", "", "passive"),
             ("R3", "1", "", "passive")),
        _net(7, "VBUS", ("J4", "A4", "VBUS", "passive"), ("C18", "1", "", "passive"),
             ("D9", "2", "A", "passive")),
        _net(8, "+5V", ("D10", "1", "K", "passive"), ("D9", "1", "K", "passive"),
             ("U2", "1", "IN", "power_in"), ("U2", "3", "EN", "input"),
             ("C10", "1", "", "passive")),
        _net(9, "+3V3", ("U2", "5", "OUT", "power_out"), ("C11", "1", "", "passive"),
             ("U5", "3", "3V3", "power_in")),
        _net(10, "Q2_G", ("Q2", "2", "G", "input"), ("R5", "2", "", "passive"),
             ("R6", "1", "", "passive"), ("D3", "2", "A", "passive")),
        _net(11, "Net-(Q3-D)", ("R6", "2", "", "passive"), ("Q3", "3", "D", "passive")),
        _net(12, "SW_CTRL", ("Q3", "1", "G", "input"), ("R7", "2", "", "passive"),
             ("R8", "1", "", "passive")),
        _net(13, "LED_EN", ("R7", "1", "", "passive"), ("U5", "24", "IO18", "bidirectional")),
        _net(14, "LED_24V", ("Q2", "3", "D", "passive"), ("F2", "1", "", "passive")),
        _net(15, "+24V_LED", ("F2", "2", "", "passive"), ("J2", "3", "", "passive")),
    ]
    return _nl(tmp_path, comps, nets)


def test_fuse_is_a_series_source_path(tmp_path):
    nl = _board(tmp_path)
    assert ("F1", "VIN") in connectivity_review._links(nl, "Net-(F1-Pad1)", False)
    assert "VIN" in connectivity_review.sourced_nets(nl)


def test_power_tree_follows_or_ing_diodes_and_fixed_ldo(tmp_path):
    nl = _board(tmp_path)
    r = pr.analyze(nl, vref={"U1": 1.0}, rail_voltages={"VIN": 24, "VBUS": 5})
    rails = {x["net"]: x for x in r["rails"]}
    assert rails["+5V"]["voltage"] == pytest.approx(5.297, abs=1e-3)   # via D10 from U1
    assert "VBUS" in rails                                              # the other OR input
    u2 = next(x for x in r["regulators"] if x["name"] == "U2")
    assert u2["vout"] == {"value": 3.3, "method": "fixed", "vref_source": "symbol description"}
    assert rails["+3V3"]["voltage"] == pytest.approx(3.3)
    # a zener is no supply link: Q2_G does not become a rail
    assert "Q2_G" not in rails


def test_power_tree_names_what_feeds_an_or_ed_rail(tmp_path):
    """OP-27: +5V has no regulator but two diodes; VBUS shows its join."""
    nl = _board(tmp_path)
    r = pr.analyze(nl, vref={"U1": 1.0}, rail_voltages={"VIN": 24, "VBUS": 5})
    rails = {x["net"]: x for x in r["rails"]}
    assert rails["+5V"]["sourced_by"] == []
    assert {(f["ref"], f["from"]) for f in rails["+5V"]["fed_via"]} == {
        ("D10", "5V_BUCK"), ("D9", "VBUS")}
    vbus = next(t for t in r["tree"] if t["rail"] == "VBUS")
    assert [(j["rail"], j["via"]["ref"]) for j in vbus["joins"]] == [("+5V", "D9")]
    assert any("not modelled" in n for n in r["dc_model"]["notes"]) if "dc_model" in r else True


def test_inrush_stops_at_a_reverse_biased_diode(tmp_path):
    nl = _board(tmp_path)
    nets, _fuses = circuit_review._downstream(nl, "VBUS")
    assert "+5V" in nets and "5V_BUCK" not in nets      # D10 blocks VBUS -> buck output


def test_esd_audit_skips_supply_nets(tmp_path):
    nl = _board(tmp_path)
    findings: list = []
    rows = circuit_review.check_esd_audit(nl, findings, {}, set())
    by = {r["ref"]: r for r in rows}
    assert "J1" not in by                                # jack pin feeds VIN through F1
    assert "+24V_LED" not in str(by.get("J2", {}))


def test_dc_model_gate_follows_switch_state_and_zener(tmp_path):
    nl = _board(tmp_path)
    known = {"VIN": (24.0, "c")}
    on = dc_model.solve(nl, known, {}, {"Q3": "on"})
    off = dc_model.solve(nl, known, {}, {"Q3": "off"})
    assert on["volts"]["Q2_G"][0] == pytest.approx(12.0)          # MMSZ5242B clamps V_GS
    assert any(c["kind"] == "zener" and c["ref"] == "D3" for c in on["clamps"])
    assert off["volts"]["Q2_G"][0] == pytest.approx(24.0)          # R5 pulls the gate to VIN
    assert off["volts"]["Net-(Q3-D)"][0] == pytest.approx(24.0)    # not forced to 0 V


def test_zener_vz_from_part_number():
    assert dc_model._vz_from_part_number("MMSZ5242B") == 12.0
    assert dc_model._vz_from_part_number("1N5231B") == 5.1
    assert dc_model._vz_from_part_number("BZX84C3V3") == 3.3
    assert dc_model._vz_from_part_number("BZT52C12") == 12.0
    assert dc_model._vz_from_part_number("SMBJ24A") is None


def test_part_ratings_carry_a_switched_load_to_its_rail(tmp_path):
    nl = _board(tmp_path)
    pt = pr.analyze(nl, vref={"U1": 1.0}, rail_voltages={"VIN": 24, "VBUS": 5})
    r = part_ratings.check(nl, pt, {}, {"+24V_LED": 5.0, "+3V3": 0.4})
    sw = r["switched_loads"][0]
    assert sw["net"] == "+24V_LED" and sw["to"] == "VIN"
    assert [p["ref"] for p in sw["parts"]] == ["F2", "Q2"]
    assert r["rail_currents"]["VIN"] >= 5.0
    nodata = {f["ref"] for f in r["findings"] if f["rule"] == "RT-NODATA"}
    assert {"Q2", "F2", "F1", "J1"} <= nodata          # every part on the 5 A path is looked at
    assert r["regulators"]["U1"]["iout"] == pytest.approx(0.4)   # +5V through D10


def test_tj_estimate_above_silicon_limit_warns_without_tj_max(tmp_path):
    nl = _board(tmp_path)
    pt = pr.analyze(nl, vref={"U1": 1.0}, rail_voltages={"VIN": 24})
    r = part_ratings.check(nl, pt, {}, {"+3V3": 0.4})
    # U2 has a "Lib:..._FP" footprint the package table does not know: no
    # Tj estimate, so only NODATA. With a SOT-23 footprint the estimate exists.
    nl.components["U2"]["footprint"] = "Package_TO_SOT_SMD:SOT-23-5"
    r = part_ratings.check(nl, pt, {}, {"+3V3": 0.4})
    tj = [f for f in r["findings"] if f["rule"] == "RT-TJ" and f["ref"] == "U2"]
    assert tj and tj[0]["severity"] == "warning" and "125" in tj[0]["message"]


def test_caller_rth_ja_wins_over_package_typical(tmp_path):
    nl = _board(tmp_path)
    nl.components["U2"]["footprint"] = "Package_TO_SOT_SMD:SOT-23-5"
    pt = pr.analyze(nl, vref={"U1": 1.0}, rail_voltages={"VIN": 24})
    r = part_ratings.check(nl, pt, {}, {"+3V3": 0.4}, rth_ja={"U2": 60.3})
    row = next(t for t in r["thermal"] if t["ref"] == "U2")
    assert row["rth_ja"] == 60.3 and row["rth_source"] == "caller"
    assert not [f for f in r["findings"] if f["rule"] == "RT-TJ" and f["ref"] == "U2"]
    assert r["assumptions"]["rth_ja_caller"] == {"U2": 60.3}


def test_vref_carries_adjustable_buck_current_upstream(tmp_path):
    nl = _board(tmp_path)
    bare = part_ratings.check(nl, pr.analyze(nl, rail_voltages={"VIN": 24}), {}, {"+3V3": 0.4})
    assert any(f["rule"] == "RT-MODEL" and f["ref"] == "U1" for f in bare["findings"])
    pt = pr.analyze(nl, vref={"U1": 1.0}, rail_voltages={"VIN": 24})
    r = part_ratings.check(nl, pt, {}, {"+3V3": 0.4})
    assert not any(f["rule"] == "RT-MODEL" and f["ref"] == "U1" for f in r["findings"])
    assert r["rail_currents"]["VIN"] > 0

# ---- OP-18: load switch, switched data line, fuse selectivity ---------------- #

from kicad_claude.adapters import switch_review  # noqa: E402


def _with_data(tmp_path, *, oe_from_command=True, oe_pull=True, pull_up=True, reset_hold=True):
    """`_board` plus the strip's data path: U4 (74AHCT1G125) on +5V, its OE
    from Q4 inverting SW_CTRL, 33R to J2.2 with a 100k pull-down."""
    from .test_review_ratings_circuits_bom import _comp, _net, _nl

    nl0 = _board(tmp_path)
    comps, nets = [], []
    for ref, c in nl0.components.items():
        if ref == "R5" and not pull_up or ref == "R8" and not reset_hold:
            continue
        comps.append(_comp(ref, c["value"], lib=c["lib_id"], desc=c.get("description", "")))
    comps += [_comp("U4", "SN74AHCT1G125", lib="74xGxx:74AHCT1G125"),
              _comp("Q4", "2N7002", lib="Transistor_FET:2N7002", desc="N-Channel MOSFET"),
              _comp("R9", "10k"), _comp("R13", "33"), _comp("R14", "100k")]
    for name, pins in nl0.nets.items():
        nodes = [(p.ref, p.number, p.name, p.type) for p in pins
                 if not (p.ref == "R5" and not pull_up or p.ref == "R8" and not reset_hold)]
        if name == "GND":
            nodes += [("U4", "3", "GND", "power_in"), ("Q4", "2", "S", "passive"),
                      ("R14", "2", "", "passive")]
        if name == "+5V":
            nodes += [("U4", "5", "VCC", "power_in")] + ([("R9", "1", "", "passive")]
                                                        if oe_pull else [])
        if name == "SW_CTRL" and oe_from_command:
            nodes += [("Q4", "1", "G", "input")]
        nets.append(_net(len(nets) + 1, name, *nodes))
    oe = [("U4", "1", "", "input"), ("Q4", "3", "D", "passive")]
    if oe_pull:
        oe.append(("R9", "2", "", "passive"))
    nets += [_net(90, "DATA_OE_N", *oe),
             _net(91, "U4_Y", ("U4", "4", "", "tri_state"), ("R13", "1", "", "passive")),
             _net(92, "DATA_OUT", ("R13", "2", "", "passive"), ("R14", "1", "", "passive"),
                  ("J2", "2", "", "passive")),
             _net(93, "LED_DATA", ("U4", "2", "", "input"),
                  ("U5", "25", "IO19", "bidirectional"))]
    if not oe_from_command:
        nets.append(_net(94, "OTHER", ("Q4", "1", "G", "input"),
                         ("U5", "26", "IO20", "bidirectional")))
    return _nl(tmp_path, comps, nets, name="d.net")


def _sw(nl, specs=None, vin=24.0):
    findings: list = []
    rows = switch_review.check_load_switches(nl, findings, specs or {}, {"VIN": vin})
    data = switch_review.check_data_enables(nl, findings, specs or {}, rows)
    return rows, data, findings


def test_load_switch_is_recognised_with_its_network(tmp_path):
    rows, data, findings = _sw(_with_data(tmp_path))
    q = rows[0]
    assert (q["ref"], q["rail"], q["load"], q["driver"], q["command"]) == \
        ("Q2", "VIN", "LED_24V", "Q3", "SW_CTRL")
    assert q["pull_up"] == ["R5"] and q["zener"] == ["D3"] and q["reset_hold"] == ["R8"]
    assert q["vgs"] == {"on": pytest.approx(-12.0), "off": pytest.approx(0.0)}
    assert data == [{"switch": "Q2", "connector": "J2", "net": "DATA_OUT", "driver": "U4.4",
                     "enable": "U4.1", "enable_default": ["R9"]}]
    assert not [f for f in findings if f["severity"] in ("error", "warning")]
    assert any(f["rule"] == "SW-SOFTSTART" for f in findings)   # no Miller cap here


def test_load_switch_faults(tmp_path):
    _rows, _d, f = _sw(_with_data(tmp_path, pull_up=False, reset_hold=False))
    rules = {x["rule"] for x in f}
    assert {"SW-OFF-DEFAULT", "SW-RESET"} <= rules


def test_gate_drive_against_the_rds_on_rating(tmp_path):
    nl = _with_data(tmp_path)
    specs = {"Q2": {"mpn": "DMP4015SPSQ", "absolute_max": {"VGS": {"min": -25, "max": 25,
                                                                   "unit": "V", "page": 2}},
                    "electrical": {"RDS_on": {"max": 0.011, "unit": "Ohm", "page": 3,
                                              "conditions": "VGS = -10 V, ID = -10 A"}}}}
    _r, _d, f = _sw(nl, specs, vin=24.0)
    assert not [x for x in f if x["rule"] == "SW-VGS-ON"]
    rows, _d, f = _sw(nl, specs, vin=8.0)          # 8 V in: V_GS = -5.4 V
    assert rows[0]["vgs"]["on"] == pytest.approx(-5.44, abs=0.02)
    assert any(x["rule"] == "SW-VGS-ON" and "10 V" in x["message"] for x in f)


def test_data_enable_must_follow_the_switch(tmp_path):
    _r, _d, f = _sw(_with_data(tmp_path, oe_from_command=False, oe_pull=False))
    rules = {x["rule"] for x in f}
    assert {"SW-OE", "SW-OE-DEFAULT"} <= rules


def test_fuse_selectivity_from_i2t(tmp_path):
    nl = _board(tmp_path)
    f: list = []
    rows = switch_review.check_fuse_selectivity(nl, f, {})
    assert rows[0]["fuse"] == "F2" and rows[0]["upstream"] == "F1"
    assert f[0]["severity"] == "info"
    good = {"F1": {"mpn": "0452007", "electrical": {"I2t": {"typ": 123.83, "unit": "A2s",
                                                            "page": 2}}},
            "F2": {"mpn": "0451007", "electrical": {"I2t": {"typ": 10.32, "unit": "A2s",
                                                            "page": 2}}}}
    f = []
    rows = switch_review.check_fuse_selectivity(nl, f, good)
    assert rows[0]["i2t_ratio"] == pytest.approx(10.32 / 123.83, abs=1e-3) and not f
    bad = {"F1": good["F2"], "F2": good["F1"]}
    f = []
    switch_review.check_fuse_selectivity(nl, f, bad)
    assert f[0]["rule"] == "SW-FUSE-SEL" and f[0]["severity"] == "warning"
