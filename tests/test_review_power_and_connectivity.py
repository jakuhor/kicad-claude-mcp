"""Design review: power tree and connectivity."""

from __future__ import annotations

import pytest

from kicad_claude.adapters import power_review as pr


def _comp(ref, value, lib="Device:R", dnp=False):
    prop = ' (property (name "dnp"))' if dnp else ""
    lib_name, part = lib.split(":")
    return (f'(comp (ref "{ref}") (value "{value}") (footprint "")'
            f' (libsource (lib "{lib_name}") (part "{part}") (description ""))'
            f' (fields){prop})')


def _net(code, name, *nodes):
    body = " ".join(
        f'(node (ref "{r}") (pin "{n}")' + (f' (pinfunction "{f}_{n}")' if f else "")
        + f' (pintype "{t}"))'
        for r, n, f, t in nodes)
    return f'(net (code "{code}") (name "{name}") {body})'


def _netlist(tmp_path):
    comps = [
        _comp("U1", "BUCK1", "Regulator_Switching:BUCK1"),
        _comp("U2", "LDO1", "Regulator_Linear:LDO1"),
        _comp("U3", "LDO2", "Regulator_Linear:LDO2"),
        _comp("L1", "10u", "Device:L"),
        _comp("L2", "2.2u", "Device:L", dnp=True),
        _comp("R1", "100k"), _comp("R2", "20k"),       # U1 FB divider
        _comp("R3", "100k"), _comp("R4", "47k"),       # U1 EN divider
        _comp("R5", "10k"),                            # U1 PG pull-up
        _comp("C1", "10u", "Device:C"), _comp("C2", "22u", "Device:C"),
        _comp("C3", "1u", "Device:C"),
        _comp("J1", "IN", "Connector:J"), _comp("J2", "OUT", "Connector:J"),
    ]
    nets = [
        _net(1, "VIN", ("J1", "1", "", "passive"), ("U1", "1", "VIN", "power_in"),
             ("R3", "1", "", "passive"), ("C1", "1", "", "passive")),
        _net(2, "GND", ("J1", "2", "", "passive"), ("U1", "2", "GND", "power_in"),
             ("R2", "2", "", "passive"), ("R4", "2", "", "passive"),
             ("C1", "2", "", "passive"), ("C2", "2", "", "passive"),
             ("C3", "2", "", "passive"), ("U2", "2", "GND", "power_in"),
             ("U3", "2", "GND", "power_in")),
        _net(3, "Net-(U1-SW)", ("U1", "3", "SW", "output"), ("L1", "1", "", "passive")),
        _net(4, "+5V", ("L1", "2", "", "passive"), ("R1", "1", "", "passive"),
             ("C2", "1", "", "passive"), ("U2", "1", "IN", "power_in"),
             ("R5", "1", "", "passive"), ("L2", "1", "", "passive")),
        _net(5, "Net-(U1-FB)", ("U1", "4", "FB", "input"), ("R1", "2", "", "passive"),
             ("R2", "1", "", "passive")),
        _net(6, "Net-(U1-EN)", ("U1", "5", "EN", "input"), ("R3", "2", "", "passive"),
             ("R4", "1", "", "passive")),
        _net(7, "PG_5V", ("U1", "6", "PG", "open_collector"), ("R5", "2", "", "passive"),
             ("U2", "3", "EN", "input")),
        _net(8, "+3V3", ("U2", "4", "OUT", "power_out"), ("C3", "1", "", "passive"),
             ("J2", "1", "", "passive")),
        _net(9, "+5V_F", ("L2", "2", "", "passive"), ("U3", "1", "IN", "power_in")),
        _net(10, "unconnected-(U3-EN-Pad3)", ("U3", "3", "EN", "input")),
        _net(11, "+1V8", ("U3", "4", "OUT", "power_out"), ("J2", "2", "", "passive")),
    ]
    text = f'(export (version "E") (components {" ".join(comps)}) (nets {" ".join(nets)}))'
    p = tmp_path / "t.net"
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize("text,value", [
    ("4k7", 4700.0), ("10k", 1e4), ("100n", 1e-7), ("2.2u", 2.2e-6),
    ("0R", 0.0), ("49.9", 49.9), ("1M", 1e6), ("49k9", 49900.0), ("abc", None),
    # OP-16: notations KiCAD's simulator reads that the parser used to miss.
    ("10 µF", 1e-5), ("10µF", 1e-5), ("10 μF", 1e-5), ("10μF", 1e-5),
    ("2.2Meg", 2.2e6), ("2.2MEG", 2.2e6), ("4,7k", 4700.0), ("0,1uF", 1e-7),
    ("0.1uF, 50V", 1e-7), ("0.1uF,50V", 1e-7), ("100nF 50V", 1e-7), ("47 nF 50 V", 4.7e-8),
    ("78 182", 78182.0), ("1 000 000", 1e6), ("4.7Ω", 4.7), ("4.7Ω", 4.7),
    ("10 ohm", 10.0), ("10kohm", 1e4), ("100nf", 1e-7), ("1fF", 1e-15), ("-5", -5.0),
    ("10k 1%", 1e4), ("1,5", 1.5),
    ("1,000", None),   # decimal comma or thousands separator: not guessed
    ("DNP", None), ("", None),
])
def test_parse_value(text, value):
    got = pr.parse_value(text)
    assert got == pytest.approx(value) if value is not None else got is None


def test_power_tree(tmp_path):
    nl = pr.parse_netlist(_netlist(tmp_path))
    assert nl.pins["U1"][0].name == "VIN"  # `_<pin>` suffix stripped
    r = pr.analyze(nl, vref={"U1": 0.8})
    regs = {x["name"]: x for x in r["regulators"]}
    assert set(regs) == {"U1", "U2", "U3"}

    u1 = regs["U1"]
    assert u1["topology"] == "switching" and u1["inductor"]["ref"] == "L1"
    assert (u1["input_net"], u1["output_net"]) == ("VIN", "+5V")
    assert u1["enable"]["state"] == "divider_from_input"
    div = u1["feedback"]["divider"]
    assert (div["top"], div["bottom"]) == ("R1", "R2") and div["ratio"] == pytest.approx(6.0)
    assert u1["vout"] == {"value": pytest.approx(4.8), "vref": 0.8, "vref_source": "caller"}
    assert u1["power_good"]["state"] == "pulled_up" and u1["power_good"]["pullup"] == ["R5"]

    u2 = regs["U2"]
    assert u2["topology"] == "linear"
    assert u2["enable"] == {"state": "driven", "net": "PG_5V", "by": ["U1.PG"]}
    assert r["sequencing"] == [{"regulator": "U2", "enabled_by": ["U1.PG"]}]

    rules = {(f["rule"], f["ref"]) for f in r["findings"]}
    assert ("PT-EN-FLOAT", "U3") in rules
    assert ("PT-DNP-LINK", "U3") in rules           # +5V_F only via DNP L2
    assert ("PT-CIN", "U3") in rules and ("PT-COUT", "U3") in rules
    assert ("PT-VOUT-VREF", "U1") not in rules       # Vref given
    assert r["counts"]["error"] == 1

    root = r["tree"][0]
    assert root["rail"] == "VIN" and root["connectors"] == ["J1"]
    five = root["regulators"][0]["output"]
    assert five["rail"] == "+5V"
    assert [c["regulator"] for c in five["regulators"]] == ["U2"]
    linked = five["linked"][0]
    assert linked["rail"] == "+5V_F" and linked["via"] == {"ref": "L2", "value": "2.2u", "dnp": True}
    assert linked["regulators"][0]["output"]["rail"] == "+1V8"


def test_vout_needs_vref(tmp_path):
    r = pr.analyze(pr.parse_netlist(_netlist(tmp_path)))
    u1 = next(x for x in r["regulators"] if x["name"] == "U1")
    assert u1["vout"] is None
    assert any(f["rule"] == "PT-VOUT-VREF" and f["ref"] == "U1" for f in r["findings"])


def test_analyze_power_tree_tool(tmp_path):
    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools.review import design as review

    mcp = FastMCP("t")
    review.register(mcp)
    res = mcp._tool_manager.get_tool("analyze_power_tree").fn(
        netlist_path=str(_netlist(tmp_path)), include_details=False)
    assert isinstance(res["regulators"][0]["input_caps"], int)
    assert "note" in res


# ---- review_connectivity --------------------------------------------------- #

from kicad_claude.adapters import connectivity_review as cr  # noqa: E402


def _cr_netlist(tmp_path):
    comps = [
        _comp("J1", "IN", "Connector:J"),
        _comp("Q1", "PFET", "Device:Q_PMOS"),
        _comp("U1", "MCU", "MCU:MCU"),
        _comp("U2", "EEPROM", "Memory_EEPROM:24LC02"),
        _comp("U3", "EEPROM", "Memory_EEPROM:24LC02"),
        _comp("U4", "SENSOR", "Sensor:S"),
        _comp("L1", "BEAD", "Device:FerriteBead", dnp=True),
        _comp("R1", "4k7"), _comp("C1", "100n", "Device:C"),
    ]
    nets = [
        _net(1, "VIN", ("J1", "1", "", "passive"), ("Q1", "1", "S", "passive")),
        _net(2, "+3V3", ("Q1", "2", "D", "passive"), ("U1", "1", "VDD", "power_in"),
             ("U2", "1", "VCC", "power_in"), ("U3", "1", "VCC", "power_in"),
             ("R1", "1", "", "passive"), ("C1", "1", "", "passive"),
             ("L1", "1", "", "passive"), ("U2", "3", "A0", "input")),
        _net(3, "GND", ("J1", "2", "", "passive"), ("U1", "2", "GND", "power_in"),
             ("U2", "2", "GND", "power_in"), ("U3", "2", "GND", "power_in"),
             ("C1", "2", "", "passive"), ("U3", "3", "A0", "input"),
             ("U4", "2", "GND", "power_in")),
        _net(4, "SDA", ("U1", "3", "I2C1_SDA", "bidirectional"),
             ("U2", "4", "SDA", "bidirectional"), ("U3", "4", "SDA", "bidirectional"),
             ("R1", "2", "", "passive")),
        _net(5, "SCL", ("U1", "4", "I2C1_SCL", "output"),
             ("U2", "5", "SCL", "input"), ("U3", "5", "SCL", "input")),
        _net(6, "+3V3_S", ("L1", "2", "", "passive"), ("U4", "1", "VDD", "power_in")),
        _net(7, "INT", ("U4", "3", "~{INT}", "open_collector"), ("U1", "5", "PA0", "input")),
        _net(8, "unconnected-(U1-VDDA-Pad6)", ("U1", "6", "VDDA", "power_in")),
    ]
    text = f'(export (version "E") (components {" ".join(comps)}) (nets {" ".join(nets)}))'
    p = tmp_path / "c.net"
    p.write_text(text, encoding="utf-8")
    return p


def test_connectivity_review(tmp_path):
    r = cr.analyze(pr.parse_netlist(_cr_netlist(tmp_path)))
    rules = {(f["rule"], f["ref"]) for f in r["findings"]}
    # +3V3 sourced from the connector through Q1's S-D channel
    u1 = next(x for x in r["power_pins"] if x["ref"] == "U1")
    assert u1["rail_sources"]["+3V3"] == "via Q1 from VIN"
    assert ("CR-PWR-NC", "U1") in rules                 # VDDA unconnected
    assert ("CR-PWR-DNP", "U4") in rules                # only via DNP L1
    assert ("CR-DEC-NONE", "U4") in rules
    assert not any(f["rule"] == "CR-DEC-NONE" and f["ref"] == "U1" for f in r["findings"])
    assert ("CR-OD-PULL", "U4") in rules                # ~{INT} without pull-up
    assert ("CR-I2C-PULLUP", "U1,U2,U3") in rules       # SCL has none
    # U2/U3 same part, different A0 strap -> no conflict
    assert not any(f["rule"] == "CR-I2C-ADDR" for f in r["findings"])
    bus = r["i2c_buses"][0]
    assert bus["pullups"]["SDA"][0]["ref"] == "R1"
    straps = {d["ref"]: d["address_pins"] for d in bus["devices"]}
    assert straps["U2"] == {"A0": "+3V3"} and straps["U3"] == {"A0": "gnd"}


def test_connectivity_i2c_conflict(tmp_path):
    p = _cr_netlist(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace(
        '(node (ref "U2") (pin "3") (pinfunction "A0_3") (pintype "input"))', ""),
        encoding="utf-8")
    text = p.read_text(encoding="utf-8").replace(
        '(node (ref "U3") (pin "3")', '(node (ref "U2") (pin "3") (pinfunction "A0_3") '
        '(pintype "input")) (node (ref "U3") (pin "3")')
    p.write_text(text, encoding="utf-8")
    r = cr.analyze(pr.parse_netlist(p), ("i2c",))
    assert any(f["rule"] == "CR-I2C-ADDR" and f["ref"] == "U2,U3" for f in r["findings"])
    assert set(r) == {"i2c_buses", "findings", "counts", "specs_used"}


def test_review_connectivity_tool(tmp_path):
    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools.review import design as review

    mcp = FastMCP("t")
    review.register(mcp)
    fn = mcp._tool_manager.get_tool("review_connectivity").fn
    res = fn(checks=["power_pins"], netlist_path=str(_cr_netlist(tmp_path)))
    assert "power_pins" in res and "i2c_buses" not in res
    with pytest.raises(ValueError):
        fn(checks=["bogus"], netlist_path=str(_cr_netlist(tmp_path)))
