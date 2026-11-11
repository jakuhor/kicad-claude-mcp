"""Domain detectors, second set (OP-11)."""

from __future__ import annotations

from kicad_claude.adapters import circuit_review
from kicad_claude.adapters import power_review as pr

from .test_review_ratings_circuits_bom import _comp, _net, _nl


def _run(tmp_path, comps, nets, groups, volts=None, specs=None):
    nl = _nl(tmp_path, comps, nets)
    pt = pr.analyze(nl, rail_voltages=volts or {})
    r = circuit_review.analyze(nl, pt, specs or {}, groups)
    return r, {(f["rule"], f["ref"]) for f in r["findings"]}


def test_ethernet_hdmi_lvds_memory_wireless(tmp_path):
    comps = [_comp("U1", "PHY"), _comp("J1", "RJ45", lib="Connector:RJ45"),
             _comp("J2", "HDMI", lib="Connector:HDMI_A"), _comp("U2", "SN65LVDS1", lib="X:LVDS_RX"),
             _comp("U3", "W25Q"), _comp("U4", "ESP32", lib="RF_Module:ESP32"),
             _comp("R1", "1k")]
    eth = [_net(10 + i, f"MDI{i}", ("U1", str(i), n, "bidirectional"), ("J1", str(i), "", "passive"))
           for i, n in enumerate(("TX+", "TX-", "RX+", "RX-"), 1)]
    hdmi = [_net(20 + i, f"T{i}", ("J2", str(i), n, "passive"))
            for i, n in enumerate(("D0+", "D0-", "D1+", "D1-", "D2+", "D2-", "CK+", "CK-"), 1)]
    nets = eth + hdmi + [
        _net(30, "SCL", ("J2", "15", "SCL", "passive")),
        _net(31, "HPD", ("J2", "19", "HPD", "passive")),
        _net(32, "LP", ("U2", "1", "INP", "input")), _net(33, "LN", ("U2", "2", "INN", "input")),
        _net(34, "unconnected-(U3-WP-Pad3)", ("U3", "3", "WP", "input")),
        _net(35, "CS", ("U3", "1", "CS", "input"), ("R1", "1", "", "passive")),
        _net(36, "SCK", ("U3", "6", "CLK", "input")), _net(37, "SI", ("U3", "5", "DI", "input")),
        _net(38, "ANT", ("U4", "1", "ANT", "bidirectional")),
    ]
    r, got = _run(tmp_path, comps, nets, ("interfaces",))
    assert ("ETH-MAG", "U1") in got
    assert ("ETH-BOBSMITH", "J1") in got
    assert ("HDMI-ESD", "J2") in got and ("HDMI-DDC", "J2") in got
    assert ("LVDS-TERM", "U2") in got
    assert ("MEM-WP", "U3") in got
    assert ("WL-ANT", "U4") in got


def test_power_conversion(tmp_path):
    comps = [_comp("U1", "CHARGER", lib="Battery_Management:MCP73831"),
             _comp("OK1", "PC817", lib="Isolator:PC817"), _comp("U2", "TL431", lib="Reference:TL431"),
             _comp("C1", "1F"), _comp("C2", "1F"),
             _comp("U3", "ISO7721", lib="Isolator:ISO7721"), _comp("R5", "0R")]
    nets = [_net(1, "VBAT", ("U1", "3", "VBAT", "power_out")),
            _net(2, "unconnected-(U1-PROG-Pad5)", ("U1", "5", "PROG", "passive")),
            _net(3, "TS", ("U1", "6", "TS", "input")),
            _net(4, "OA", ("OK1", "1", "A", "passive"), ("U2", "3", "K", "passive")),
            _net(5, "+12V", ("OK1", "2", "K", "passive")),
            _net(6, "OC", ("OK1", "4", "C", "passive")), _net(7, "OE", ("OK1", "3", "E", "passive")),
            _net(8, "REF", ("U2", "1", "REF", "passive")),
            _net(9, "GND", ("U2", "2", "A", "passive"), ("C2", "2", "", "passive"),
                 ("U3", "4", "GND1", "power_in"), ("R5", "1", "", "passive")),
            _net(10, "SC_TOP", ("C1", "1", "", "passive")),
            _net(11, "SC_MID", ("C1", "2", "", "passive"), ("C2", "1", "", "passive")),
            _net(12, "GNDISO", ("U3", "5", "GND2", "power_in"), ("R5", "2", "", "passive")),
            _net(13, "VISO", ("U3", "8", "VDD2", "power_in"))]
    r, got = _run(tmp_path, comps, nets, ("power_conversion",))
    assert ("CHG-PROG", "U1") in got and ("CHG-TS", "U1") in got
    assert ("TF-OPTO-R", "OK1") not in got        # TL431 drives the LED cathode side
    assert ("TF-COMP", "U2") in got
    assert ("SC-BAL", "C1") in got
    assert ("ISO-BRIDGE", "U3") in got and ("ISO-DEC", "U3") in got


def test_drives_and_sensing(tmp_path):
    comps = [_comp("U1", "DRV8871"), _comp("Q1", "NMOS", lib="Device:Q_NMOS_GSD"),
             _comp("L1", "10m"), _comp("U2", "MAX31865"), _comp("R1", "430", {"Tolerance": "1%"}),
             _comp("D1", "WS2812B", lib="LED:WS2812B"), _comp("D2", "WS2812B", lib="LED:WS2812B"),
             _comp("U3", "MCU")]
    nets = [_net(1, "VM", ("U1", "1", "VM", "power_in")),
            _net(2, "OUT1", ("U1", "2", "OUT1", "output")), _net(3, "OUT2", ("U1", "3", "OUT2", "output")),
            _net(4, "unconnected-(U1-BOOT-Pad4)", ("U1", "4", "BOOT", "passive")),
            _net(5, "GATE_CAP", ("U1", "5", "CBOOT", "passive")),
            _net(6, "GND", ("Q1", "2", "S", "passive"), ("U3", "9", "GND", "power_in")),
            _net(7, "COIL", ("Q1", "3", "D", "passive"), ("L1", "1", "", "passive")),
            _net(8, "+12V", ("L1", "2", "", "passive")),
            _net(9, "RA", ("U2", "1", "REFIN+", "input"), ("R1", "1", "", "passive")),
            _net(10, "RB", ("U2", "2", "REFIN-", "input"), ("R1", "2", "", "passive")),
            _net(11, "F", ("U2", "3", "FORCE+", "output")),
            _net(12, "+5V", ("D1", "1", "VDD", "power_in"), ("D2", "1", "VDD", "power_in")),
            _net(13, "+3V3", ("U3", "1", "VDD", "power_in")),
            _net(14, "DATA", ("U3", "2", "PA0", "output"), ("D1", "2", "DIN", "input")),
            _net(15, "D12", ("D1", "3", "DOUT", "output"), ("D2", "2", "DIN", "input")),
            _net(16, "unconnected-(D2-DOUT-Pad3)", ("D2", "3", "DOUT", "output"))]
    r, got = _run(tmp_path, comps, nets, ("drives",), volts={"+5V": 5.0, "+3V3": 3.3})
    assert ("MD-BOOT", "U1") in got and ("MD-VM", "U1") in got
    assert ("MD-FLYBACK", "Q1") in got
    assert ("RTD-RREF", "R1") in got              # 1 % reference resistor
    assert ("ALED-LEVEL", "D1") in got and ("ALED-R", "D1") in got
    assert len(r["addressable_leds"]) == 3        # 2 LEDs + chain summary


def test_rf_and_certification(tmp_path):
    comps = [_comp("U1", "LNA"), _comp("J1", "SMA", lib="Connector:SMA"),
             _comp("L1", "3n3"), _comp("C1", "1p"), _comp("C2", "1p2")]
    nets = [_net(1, "RFI", ("U1", "1", "RF_IN", "input"), ("L1", "1", "", "passive"),
                 ("C1", "1", "", "passive")),
            _net(2, "RFJ", ("L1", "2", "", "passive"), ("C2", "1", "", "passive"),
                 ("J1", "1", "", "passive")),
            _net(3, "GND", ("C1", "2", "", "passive"), ("C2", "2", "", "passive"),
                 ("J1", "2", "", "passive"))]
    r, got = _run(tmp_path, comps, nets, ("rf",))
    assert ("RF-MATCH", "U1") in got and r["rf"][0]["matching"] == "pi"
    assert ("RF-DCBLOCK", "U1") in got
    assert ("CERT-RADIO", "-") in got
