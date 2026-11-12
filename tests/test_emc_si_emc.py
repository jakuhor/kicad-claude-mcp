"""EMC pre-compliance review on the filled copper (OP-1)."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest

from kicad_claude.adapters import emc_review as er
from kicad_claude.adapters import sch_io
from kicad_claude.adapters.power_review import Netlist, Pin

LAYERS = '(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
STACKUP = ('(setup (stackup (layer "F.Cu" (type "copper")) (layer "dielectric 1" (type "prepreg") '
           '(thickness 0.2) (epsilon_r 4.5)) (layer "In1.Cu" (type "copper")) (layer "dielectric 2" '
           '(type "core") (thickness 1.0) (epsilon_r 4.5)) (layer "In2.Cu" (type "copper")) '
           '(layer "dielectric 3" (type "prepreg") (thickness 0.2) (epsilon_r 4.5)) '
           '(layer "B.Cu" (type "copper"))))')


def rect(x0, y0, x1, y1):
    return f"(pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))"


def zone(net, layer, x0, y0, x1, y1):
    r = rect(x0, y0, x1, y1)
    return (f'(zone (net "{net}") (layer "{layer}") (polygon {r}) '
            f'(filled_polygon (layer "{layer}") {r}))')


def fp(ref, x, y, pads, value="", lib="Lib:FP"):
    ps = " ".join(f'(pad "{n}" smd rect (at {dx} {dy}) (size 0.6 0.6) (layers "F.Cu") (net "{net}"))'
                  for n, dx, dy, net in pads)
    return (f'(footprint "{lib}" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}") '
            f'(property "Value" "{value}") {ps})')


def via(x, y, net):
    return f'(via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "{net}"))'


def seg(x0, y0, x1, y1, net, layer="F.Cu", w=0.25):
    return (f'(segment (start {x0} {y0}) (end {x1} {y1}) (width {w}) (layer "{layer}") '
            f'(net "{net}"))')


def board(*items, w=40, h=20):
    outline = f'(gr_rect (start 0 0) (end {w} {h}) (layer "Edge.Cuts"))'
    text = (f'(kicad_pcb (version 20260206) {LAYERS} {STACKUP} {outline} '
            + " ".join(items) + ")")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "b.kicad_pcb"
        p.write_text(text, encoding="utf-8")
        return sch_io.parse_file(p)


def by_rule(r):
    out = {}
    for f in r["findings"]:
        out.setdefault(f["rule"], []).append(f)
    return out


# ---- primitives ------------------------------------------------------------- #


def test_raster_even_odd_hole():
    g = er.Grid(0, 0, 0.5, 40, 40)
    outer = [(0, 0), (20, 0), (20, 20), (0, 20)]
    hole = [(5, 5), (15, 5), (15, 15), (5, 15)]
    m = g.raster([outer, hole])
    assert sum(m) * 0.25 == pytest.approx(400 - 100, rel=0.02)
    assert not g.get(m, 10, 10) and g.get(m, 2, 2)


def test_components_and_formulas():
    m = bytearray([1, 1, 0, 1,
                   0, 0, 0, 1])
    comps = er.components(m, 4, 2)
    assert sorted(len(c) for c in comps) == [2, 2]
    assert er.trapezoid_harmonic(1, 1.0, 0.5, 0, 1e6) == pytest.approx(2 / math.pi)
    assert er.emission_limit(50e6, "fcc-class-b") == (40.0, 3)
    assert er.emission_limit(500e6, "cispr-class-b") == (37.0, 10)
    assert er.lambda_over_20_mm(200e6, 4.5) == pytest.approx(35.33, rel=0.01)


# ---- ground & stackup --------------------------------------------------------- #


def test_layer_roles_and_reference():
    tree = board(
        zone("GND", "F.Cu", 0, 0, 40, 12),            # 60 % pour on a routed layer
        zone("GND", "In1.Cu", 0, 0, 40, 20),          # plane
        seg(2, 16, 38, 16, "SIG"),
        seg(2, 5, 38, 5, "SIG2", layer="B.Cu"),       # B: routed, neighbour In2 empty
        via(1, 1, "GND"), via(39, 19, "GND"))
    r = er.analyze(tree, ("ground",))
    roles = {x["layer"]: x for x in r["ground"]["layers"]}
    assert roles["F.Cu"]["role"] == "signal" and roles["F.Cu"]["ground_fill"] == pytest.approx(0.6, abs=0.03)
    assert roles["In1.Cu"]["role"] == "ground_plane"
    assert roles["F.Cu"]["reference"] == [{"layer": "In1.Cu", "dielectric_mm": 0.2}]
    f = by_rule(r)
    assert [x["layer"] for x in f["EM-STACK-REF"]] == ["B.Cu"]


def test_island_split_and_bridge():
    items = [
        zone("GND", "In1.Cu", 0, 0, 20, 20), zone("+3V3", "In1.Cu", 20, 0, 40, 20),
        zone("GND", "B.Cu", 0, 0, 40, 20),
        zone("GND", "In2.Cu", 30, 2, 36, 8),          # floating island, no via
        seg(5, 10, 35, 10, "CLK"),                    # crosses GND -> +3V3 on In1
        via(2, 2, "GND"), via(38, 18, "GND"),
    ]
    r = er.analyze(board(*items), ("ground",))
    f = by_rule(r)
    assert f["EM-SPLIT-CROSS"][0]["from"] == "GND" and f["EM-SPLIT-CROSS"][0]["to"] == "+3V3"
    assert any(i["layer"] == "In2.Cu" for i in r["ground"]["islands"])
    # a capacitor joining GND and +3V3 next to the crossing bridges it
    cap = fp("C9", 20, 11, [("1", -0.5, 0, "+3V3"), ("2", 0.5, 0, "GND")])
    r = er.analyze(board(*items, cap), ("ground",))
    assert "EM-SPLIT-CROSS" not in by_rule(r)


def test_stitching_domains_connector():
    items = [zone("GND", "In1.Cu", 0, 0, 40, 20), zone("GND", "B.Cu", 0, 0, 40, 20),
             via(1, 1, "GND"),
             fp("L9", 10, 10, [("1", 0, 0, "GND"), ("2", 1, 0, "GND_USB")], "CMC"),
             fp("J1", 5, 5, [(str(k), k, 0, f"D{k}") for k in range(1, 7)] + [("7", 7, 0, "GND")],
                "USB_C")]
    r = er.analyze(board(*items), ("ground",), stitch_spacing_mm=10)
    f = by_rule(r)
    assert r["ground"]["stitching"]["coverage"] < 0.9 and "EM-STITCH" in f
    assert r["ground"]["ground_joins"] == [{"ref": "L9", "value": "CMC",
                                            "ground_nets": ["GND", "GND_USB"]}]
    assert f["EM-CONN-GND"][0]["severity"] == "warning"     # USB, 1 GND for 6 signals
    r = er.analyze(board(*items[:2], *[via(x, y, "GND") for x in range(2, 40, 5)
                                       for y in range(2, 20, 5)]), ("ground",),
                   stitch_spacing_mm=10)
    assert r["ground"]["stitching"]["coverage"] == pytest.approx(1.0)


def test_no_ground_and_unfilled():
    tree = board('(zone (net "GND") (layer "B.Cu") (polygon ' + rect(0, 0, 40, 20) + "))")
    f = by_rule(er.analyze(tree, ("ground",)))
    assert "EM-GND-NONE" in f and "EM-ZONES-UNFILLED" in f


# ---- switching ------------------------------------------------------------- #


def _sw_case(cap_x=11.5):
    tree = board(
        zone("GND", "In1.Cu", 0, 0, 40, 20),
        fp("U1", 10, 10, [("1", -1, 0, "VIN"), ("2", 1, 0, "GND"), ("3", 0, 1, "SW")], "BUCK"),
        fp("C1", cap_x, 8, [("1", -0.5, 0, "VIN"), ("2", 0.5, 0, "GND")], "10u"),
        fp("L1", 15, 12, [("1", -1, 0, "SW"), ("2", 1, 0, "+5V")], "4u7"),
        fp("J1", 38, 12, [("1", 0, 0, "+5V"), ("2", 0, 1, "GND")], "OUT"),
        seg(10, 11, 14, 12, "SW", w=0.5))
    nl = Netlist()
    for ref, num, name, net in (("U1", "1", "VIN", "VIN"), ("U1", "2", "GND", "GND"),
                                ("U1", "3", "SW", "SW"), ("L1", "1", "", "SW"),
                                ("L1", "2", "", "+5V")):
        p = Pin(ref, num, name, "passive", net)
        nl.pins[ref].append(p)
        nl.nets.setdefault(net, []).append(p)
    power = {"regulators": [{"name": "U1", "ref": "U1", "channel": None, "value": "BUCK",
                             "topology": "switching", "input_net": "VIN", "output_net": "+5V",
                             "switch_net": "SW", "inductor": {"ref": "L1", "other_net": "+5V"}}],
             "rail_voltages": {"VIN": {"volts": 12.0}, "+5V": {"volts": 5.0}}}
    return tree, nl, power


def test_switching_group():
    tree, nl, power = _sw_case()
    specs = {"U1": {"regulator": {"fsw": {"typ": 5e5, "conditions": "RT floating"}},
                    "recommended": {"IOUT": {"max": 3, "page": 5}}}}
    r = er.analyze(tree, ("switching",), power=power, nl=nl, specs=specs)
    f = by_rule(r)
    assert "RT floating" in f["EM-SW-FSW"][0]["message"]          # hint, not used
    assert "harmonics" not in r["switching"][0]

    r = er.analyze(tree, ("switching",), power=power, nl=nl, specs=specs, fsw={"U1": 1e6})
    s = r["switching"][0]
    assert s["switch_nets"] == ["SW"]
    assert s["switch_node_copper"]["SW"]["track_mm2"] == pytest.approx(0.5 * math.hypot(4, 1), abs=0.06)
    assert s["input_loop"]["cap"] == "C1" and s["input_loop"]["area_mm2"] > 0
    h = s["harmonics"]
    assert h["duty"] == pytest.approx(5 / 12, abs=1e-3) and h["duty_source"].startswith("Vout/Vin")
    assert h["first_harmonic_from_30MHz"] == 30
    est = s["radiation_estimate"]
    assert est["load_current_a"] == 3.0 and "worst case" in est["load_source"]
    assert est["worst"]["margin_db"] > 0
    assert s["inductors"][0]["ref"] == "L1"

    # far input cap and an inductor next to the edge / connector
    tree, nl, power = _sw_case(cap_x=25)
    r = er.analyze(tree, ("switching",), power=power, nl=nl, fsw={"U1": 1e6}, iout={"U1": 1})
    assert "EM-SW-CIN" in by_rule(r)
    assert r["switching"][0]["radiation_estimate"]["load_source"] == "caller"


def test_switching_needs_netlist():
    tree, _, _ = _sw_case()
    assert "EM-SW-NONET" in by_rule(er.analyze(tree, ("switching",)))
    with pytest.raises(ValueError):
        er.analyze(tree, ("ground",), standard="nope")


def test_harmonic_limit_screen():
    tree, nl, power = _sw_case()
    profile = {"name": "test profile", "unit": "dBuV/m", "distance_m": 3,
               "bands": [{"name": "B1", "f_start_hz": 30e6, "f_stop_hz": 88e6, "limit_db": -20},
                         {"name": "B2", "f_start_hz": 88e6, "f_stop_hz": 216e6,
                          "limit_db": 200, "limit_stop_db": 180}]}
    r = er.analyze(tree, ("switching",), power=power, nl=nl, fsw={"U1": 1e6},
                   iout={"U1": 1}, harmonic_limits=profile)
    hs = r["harmonic_screen"]
    assert hs["screening_only"] is True and hs["unit"] == "dBuV/m"
    b1, b2 = hs["regulators"]["U1"]
    assert b1["margin_db"] < 0 and b2["margin_db"] > 0          # below / far above the envelope
    assert 30e6 <= b1["f_hz"] <= 88e6
    assert "EM-HARM-LIMIT" in by_rule(r)
    cur = er.analyze(tree, ("switching",), power=power, nl=nl, fsw={"U1": 1e6}, iout={"U1": 1},
                     harmonic_limits={"name": "I", "unit": "dBuA",
                                      "bands": [{"f_start_hz": 1e6, "f_stop_hz": 3e6,
                                                 "limit_db": 300}]})
    assert cur["harmonic_screen"]["regulators"]["U1"][0]["harmonic"] in (1, 2, 3)
    for bad, msg in (({"unit": "dBuV/m"}, "name"),
                     ({"name": "x", "unit": "V"}, "unit"),
                     ({"name": "x", "unit": "dBuV/m", "bands": []}, "distance_m"),
                     ({"name": "x", "unit": "dBuA", "bands": [{"f_start_hz": 5,
                                                               "f_stop_hz": 1, "limit_db": 0}]},
                      "f_start_hz")):
        with pytest.raises(ValueError, match=msg):
            er.analyze(tree, ("switching",), power=power, nl=nl, harmonic_limits=bad)


# ---- decoupling & PDN ------------------------------------------------------------ #


def _nl(pins):
    nl = Netlist()
    for ref, num, name, typ, net in pins:
        p = Pin(ref, num, name, typ, net)
        nl.pins[ref].append(p)
        nl.nets.setdefault(net, []).append(p)
    return nl


def test_via_layer_change_stitching():
    # OP-18 / kicad-happy RP-001: F.Cu -> B.Cu moves the return from In1 to In2
    planes = (zone("GND", "In1.Cu", 0, 0, 40, 20), zone("GND", "In2.Cu", 0, 0, 40, 20))
    sig = (seg(5, 10, 20, 10, "SCLK"), seg(20, 10, 35, 10, "SCLK", layer="B.Cu"),
           via(20, 10, "SCLK"))
    f = by_rule(er.analyze(board(*planes, *sig), ("ground",), plan=False))
    st = f["EM-VIA-STITCH"]
    assert len(st) == 1 and st[0]["severity"] == "warning" and "In1.Cu / In2.Cu" in st[0]["message"]
    # a ground via 0.8 mm away (radius = 2 x 0.2 mm, at least 1 mm) stitches it
    f = by_rule(er.analyze(board(*planes, *sig, via(20.8, 10, "GND")), ("ground",), plan=False))
    assert "EM-VIA-STITCH" not in f
    # F.Cu -> In2.Cu: both reference In1, the return stays put
    sig2 = (seg(5, 10, 20, 10, "SCLK"), seg(20, 10, 35, 10, "SCLK", layer="In2.Cu"),
            via(20, 10, "SCLK"))
    r = er.analyze(board(zone("GND", "In1.Cu", 0, 0, 40, 20), *sig2), ("ground",), plan=False)
    assert r["ground"]["via_transitions"] == [] and "EM-VIA-STITCH" not in by_rule(r)
    # ... unless B.Cu has a pour 0.2 mm under In2 (In1 is 1.0 mm away): In2 then
    # refers to B.Cu and the return moves In1 -> B.Cu (firbox stackup)
    r = er.analyze(board(zone("GND", "In1.Cu", 0, 0, 40, 20), zone("GND", "B.Cu", 0, 0, 40, 20),
                         *sig2), ("ground",), plan=False)
    st = by_rule(r)["EM-VIA-STITCH"]
    assert len(st) == 1 and "In1.Cu / B.Cu" in st[0]["message"]
    assert "2.4 mm" in st[0]["message"]           # 2x the In1-B spacing (1.0 + 0.2 mm)


def test_via_stitch_supply_nets_and_plane_nets():
    def sig(n):
        return seg(5, 10, 20, 10, n), seg(20, 10, 35, 10, n, layer="B.Cu"), via(20, 10, n)
    # a supply trace changing layers: its AC return moves too (info)
    g = (zone("GND", "In1.Cu", 0, 0, 40, 20), zone("GND", "In2.Cu", 0, 0, 40, 20))
    st = by_rule(er.analyze(board(*g, *sig("+3V3")), ("ground",), plan=False))["EM-VIA-STITCH"]
    assert st[0]["severity"] == "info" and st[0]["message"].startswith("Supply net +3V3")
    # GND above, +5V below: no via can join them, a cap between the nets can
    gp = (zone("GND", "In1.Cu", 0, 0, 40, 20), zone("+5V", "In2.Cu", 0, 0, 40, 20))
    st = by_rule(er.analyze(board(*gp, *sig("SCLK")), ("ground",), plan=False))["EM-VIA-STITCH"]
    assert "add a capacitor between the two plane nets" in st[0]["message"]
    cap = fp("C1", 22, 10, [("1", 0, 0, "+5V"), ("2", 1, 0, "GND")], "100n")
    r = er.analyze(board(*gp, *sig("SCLK"), cap), ("ground",), plan=False)
    assert "EM-VIA-STITCH" not in by_rule(r)
    # GNDA planes: a GND via next to it is no stitch
    ga = (zone("GNDA", "In1.Cu", 0, 0, 40, 20), zone("GNDA", "In2.Cu", 0, 0, 40, 20))
    r = er.analyze(board(*ga, *sig("SCLK"), via(20.8, 10, "GND")), ("ground",), plan=False)
    assert "no GNDA via" in by_rule(r)["EM-VIA-STITCH"][0]["message"]
    r = er.analyze(board(*ga, *sig("SCLK"), via(20.8, 10, "GNDA")), ("ground",), plan=False)
    assert "EM-VIA-STITCH" not in by_rule(r)


def test_reference_gap():
    # OP-19 / kicad-happy GP-001: In1 GND with a 4 mm slot at x 18..22
    plane = (zone("GND", "In1.Cu", 0, 0, 18, 20), zone("GND", "In1.Cu", 22, 0, 40, 20))
    r = er.analyze(board(*plane, seg(5, 10, 35, 10, "SIG"), seg(5, 5, 35, 5, "SCLK"),
                         seg(5, 15, 15, 15, "OK")), ("ground",), plan=False)
    gaps = {g["net"]: g for g in r["ground"]["reference_gaps"]}
    assert set(gaps) == {"SIG", "SCLK"}                      # OK never leaves the plane
    assert gaps["SIG"]["longest_gap_mm"] == pytest.approx(4, abs=0.6)
    assert gaps["SIG"]["layer"] == "F.Cu" and gaps["SIG"]["reference_mm"] == 0.2
    sev = {f["net"]: f["severity"] for f in by_rule(r)["EM-REF-GAP"]}
    assert sev == {"SCLK": "warning", "SIG": "info"}           # clock, or a stretch >= 5 mm
    # a 6 mm slot is a warning on any signal 0.2 mm above its plane;
    # a via's antipad is not a gap
    wide = (zone("GND", "In1.Cu", 0, 0, 17, 20), zone("GND", "In1.Cu", 23, 0, 40, 20))
    f = by_rule(er.analyze(board(*wide, seg(5, 10, 35, 10, "SIG")), ("ground",), plan=False))
    assert f["EM-REF-GAP"][0]["severity"] == "warning"
    tree = board(zone("GND", "In1.Cu", 0, 0, 40, 20), seg(5, 10, 20, 10, "SIG"), via(20, 10, "SIG"))
    assert er.analyze(tree, ("ground",), plan=False)["ground"]["reference_gaps"] == []


def test_reference_gap_physics():
    # OP-20 #5-12
    slot = (zone("GND", "In1.Cu", 0, 0, 18, 20), zone("GND", "In1.Cu", 22, 0, 40, 20))

    def gaps(*items):
        r = er.analyze(board(*items), ("ground",), plan=False)
        return {g["net"]: g for g in r["ground"]["reference_gaps"]}, by_rule(r)
    # the 4 mm gap measured whole although the track is cut into 1 mm segments
    pieces = [seg(x, 10, x + 1, 10, "SIG") for x in range(5, 35)]
    g, _ = gaps(*slot, *pieces)
    assert g["SIG"]["longest_gap_mm"] == pytest.approx(4, abs=0.6)
    # a signal-net pour under the slot is no reference; a GND pour there is
    g, _ = gaps(*slot, zone("SIG2", "In1.Cu", 18, 0, 22, 20), seg(5, 10, 35, 10, "SIG"))
    assert "SIG" in g
    g, _ = gaps(*slot, zone("GND", "In1.Cu", 18, 0, 22, 20), seg(5, 10, 35, 10, "SIG"))
    assert "SIG" not in g
    # In2.Cu track: B.Cu GND under the In1 slot is a local reference
    g, _ = gaps(*slot, zone("GND", "B.Cu", 0, 0, 40, 20), seg(5, 10, 35, 10, "SIG", layer="In2.Cu"))
    assert "SIG" not in g
    # a supply trace over the gap is listed as info
    g, f = gaps(*slot, seg(5, 10, 35, 10, "+3V3"))
    assert g["+3V3"]["kind"] == "supply" and f["EM-REF-GAP"][0]["severity"] == "info"
    # four 0.5 mm nicks add up to 2 mm but no stretch reaches the 1 mm floor
    nicks = [zone("GND", "In1.Cu", x, 0, x + 4.5, 20) for x in (0, 5, 10, 15, 20)]
    g, _ = gaps(*nicks, seg(2, 10, 24, 10, "SIG"))
    assert "SIG" not in g


def test_via_stitch_fanout_tht_own_net_and_split():
    # OP-20 #15-18
    g = (zone("GND", "In1.Cu", 0, 0, 40, 20), zone("GND", "In2.Cu", 0, 0, 40, 20))
    # via in an SMD pad on F.Cu, track only on B.Cu: still a layer change
    pad = fp("C1", 20, 10, [("1", 0, 0, "SIG"), ("2", 1, 0, "GND")], "100n")
    r = er.analyze(board(*g, pad, seg(20, 10, 35, 10, "SIG", layer="B.Cu"), via(20, 10, "SIG")),
                   ("ground",), plan=False)
    assert [t["layers"] for t in r["ground"]["via_transitions"]] == [["F.Cu", "B.Cu"]]
    # a through-hole connector pin: top in, bottom out
    tht = ('(footprint "Conn:Pin" (layer "F.Cu") (at 20 10) (property "Reference" "J1") '
           '(pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") '
           '(net "SIG")))')
    r = er.analyze(board(*g, tht, seg(5, 10, 20, 10, "SIG"), seg(20, 10, 35, 10, "SIG",
                                                                 layer="B.Cu")),
                   ("ground",), plan=False)
    assert [t["via"] for t in r["ground"]["via_transitions"]] == ["J1.1"]
    # a +5V via landing in a +5V pour on In1: its own plane is no reference for it
    own = (zone("+5V", "In1.Cu", 0, 0, 40, 20), zone("GND", "In2.Cu", 0, 0, 40, 20))
    r = er.analyze(board(*own, seg(5, 10, 20, 10, "+5V"), seg(20, 10, 35, 10, "+5V", layer="B.Cu"),
                         via(20, 10, "+5V")), ("ground",), plan=False)
    assert all("+5V" not in t["reference_nets"] for t in r["ground"]["via_transitions"])
    # GND above, GND_ISO below: a split, not a capacitor to add
    iso = (zone("GND", "In1.Cu", 0, 0, 40, 20), zone("GND_ISO", "In2.Cu", 0, 0, 40, 20))
    r = er.analyze(board(*iso, seg(5, 10, 20, 10, "SIG"), seg(20, 10, 35, 10, "SIG", layer="B.Cu"),
                         via(20, 10, "SIG")), ("ground",), plan=False)
    m = by_rule(r)["EM-VIA-STITCH"][0]["message"]
    assert "two ground nets" in m and "non-safety" in m


def test_fsw_from_rt_strap():
    hint = {"typ": 1.8e6, "conditions": "RT = 46.4k"}
    nl = _nl([("U2", "5", "RT", "passive", "RTN"), ("R28", "1", "", "passive", "RTN"),
              ("R28", "2", "", "passive", "GND"), ("TP1", "1", "", "passive", "RTN")])
    nl.components = {"R28": {"value": "46.4k"}, "TP1": {"value": "TestPoint"}}
    f, why = er.fsw_from_strap(nl, "U2", hint)
    assert f == 1.8e6 and "R28 46.4k" in why
    # SYNC driven by an MCU clock: RT no longer sets fsw
    nl_s = _nl([("U2", "5", "RT", "passive", "RTN"), ("R28", "1", "", "passive", "RTN"),
                ("R28", "2", "", "passive", "GND"), ("U2", "6", "SYNC", "input", "CLK"),
                ("U9", "3", "PWM", "output", "CLK")])
    nl_s.components = {"R28": {"value": "46.4k"}}
    assert er.fsw_from_strap(nl_s, "U2", hint) is None
    nl.components["R28"]["value"] = "30.9k"                 # another frequency
    assert er.fsw_from_strap(nl, "U2", hint) is None
    floating = {"typ": 5e5, "conditions": "RT floating"}
    nl2 = _nl([("U1", "3", "RT", "passive", "unconnected-(U1-RT-Pad3)")])
    nl2.components = {}
    assert er.fsw_from_strap(nl2, "U1", floating)[0] == 5e5
    assert er.fsw_from_strap(nl, "U2", floating) is None    # a resistor is fitted
    # OP-20 #43-44: "SYNC floating, RT = 46.4k" asks for the resistor, not a floating RT;
    # a SYNCOUT pin and an EN/SYNC tied to a rail drive no clock in
    assert er.fsw_from_strap(nl2, "U1", {"typ": 5e5, "conditions": "SYNC floating, RT = 46.4k"})         is None
    nl_o = _nl([("U2", "5", "RT", "passive", "RTN"), ("R28", "1", "", "passive", "RTN"),
                ("R28", "2", "", "passive", "GND"), ("U2", "6", "SYNCOUT", "output", "CLKO"),
                ("U9", "3", "IN", "input", "CLKO"), ("U2", "7", "EN/SYNC", "input", "+5V"),
                ("U8", "2", "VOUT", "power_out", "+5V")])
    nl_o.components = {"R28": {"value": "46.4k"}}
    assert er.fsw_from_strap(nl_o, "U2", hint)[0] == 1.8e6


def test_decoupling_group():
    tree = board(
        zone("GND", "In1.Cu", 0, 0, 40, 20),
        fp("U1", 10, 10, [("1", 0, 0, "+3V3"), ("2", 0, 1, "GND"), ("3", 5, 0, "VIO")], "MCU"),
        fp("C1", 11, 11, [("1", 0, 0, "+3V3"), ("2", 1, 0, "GND")], "100n"),
        fp("C2", 30, 10, [("1", 0, 0, "VIO"), ("2", 1, 0, "GND")], "10u"),
        via(12.2, 11, "GND"))
    nl = _nl([("U1", "1", "VDD", "power_in", "+3V3"), ("U1", "2", "GND", "power_in", "GND"),
              ("U1", "3", "VDDIO", "power_in", "VIO"), ("U1", "4", "AVDD", "power_in", "+1V8")])
    power = {"regulators": [], "rail_voltages": {"+3V3": {"volts": 3.3}}}
    r = er.analyze(tree, ("decoupling",), power=power, nl=nl,
                   pdn={"+3V3": {"ripple": 0.03, "i_step": 0.1}}, plan=False)
    d = r["decoupling"]
    pins = {p["pin"]: p for p in d["supply_pins"]}
    assert pins["VDD(1)"]["nearest_cap"] == "C1" and pins["VDD(1)"]["distance_mm"] < 3
    assert pins["VDDIO(3)"]["nearest_cap"] == "C2"
    assert "AVDD(4)" not in pins                         # no pad on the board
    f = by_rule(r)
    assert [x["ref"] for x in f["EM-DEC-DIST"]] == ["U1"] and "VDDIO" in f["EM-DEC-DIST"][0]["message"]
    vias = {c["ref"]: c for c in d["cap_vias"]}
    assert vias["C1"]["gnd_via_mm"] == pytest.approx(0.2, abs=0.01)
    assert any(x["ref"] == "C2" for x in f["EM-DEC-VIA"])
    bank = d["banks"]["+3V3"]
    assert bank["c_total_f"] == pytest.approx(1e-7)
    # OP-18: impedance profile with no target; 100 kHz is the capacitive asymptote
    assert set(bank["z_profile_ohm"]) == {"100000", "1e+06", "1e+07", "1e+08"}
    assert bank["z_profile_ohm"]["100000"] == pytest.approx(1 / (2 * math.pi * 1e5 * 1e-7), rel=0.01)
    assert "ESR missing" in bank["z_profile_basis"]          # no datasheet ESR here
    assert bank["z_profile_ohm"]["1e+08"] is None               # above 0.3x resonance: unknown
    assert "z_profile_ohm" in d["banks"]["VIO"]                  # no pdn target for VIO
    assert bank["z_target_ohm"] == pytest.approx(3.3 * 0.03 / 0.1)
    assert bank["meets_target_above_hz"] == round(1 / (2 * math.pi * 1e-7 * bank["z_target_ohm"]))
    assert "ESL known for 0/1" in f["EM-PDN"][0]["message"]
    # a supply net with no cap at all
    tree2 = board(zone("GND", "In1.Cu", 0, 0, 40, 20),
                  fp("U1", 10, 10, [("1", 0, 0, "+3V3"), ("2", 0, 1, "GND")], "MCU"))
    r = er.analyze(tree2, ("decoupling",), power=power, nl=nl, plan=False)
    assert "EM-DEC-NONE" in by_rule(r)
    # caps there but DNP on the board (stale flags): said so, not "no capacitor"
    c9 = fp("C9", 11, 11, [("1", 0, 0, "+3V3"), ("2", 1, 0, "GND")], "100n").replace(
        '(property "Value"', '(attr smd dnp) (property "Value"')
    tree3 = board(zone("GND", "In1.Cu", 0, 0, 40, 20),
                  fp("U1", 10, 10, [("1", 0, 0, "+3V3"), ("2", 0, 1, "GND")], "MCU"), c9)
    msg = by_rule(er.analyze(tree3, ("decoupling",), power=power, nl=nl, plan=False))
    assert "only capacitors marked DNP on the board (C9)" in msg["EM-DEC-NONE"][0]["message"]


def test_decoupling_findings_group_per_ic_and_net():
    # OP-18: 3 VDD pins on one rail, no cap -> one EM-DEC-NONE naming all three
    tree = board(zone("GND", "In1.Cu", 0, 0, 40, 20),
                 fp("U1", 10, 10, [("1", 0, 0, "+1V8"), ("2", 0, 1, "+1V8"), ("3", 0, 2, "+1V8"),
                                   ("4", 0, 3, "GND")], "BGA"))
    nl = _nl([("U1", n, "VDD_IO", "power_in", "+1V8") for n in ("1", "2", "3")]
             + [("U1", "4", "GND", "power_in", "GND")])
    r = er.analyze(tree, ("decoupling",), power={"regulators": [], "rail_voltages": {}},
                   nl=nl, plan=False)
    none = by_rule(r)["EM-DEC-NONE"]
    assert len(none) == 1 and "VDD_IO(1), VDD_IO(2), VDD_IO(3) on +1V8" in none[0]["message"]
    assert len(r["decoupling"]["supply_pins"]) == 3          # per-pin data kept


# ---- I/O, signals, test plan ------------------------------------------------------- #


def test_io_group_and_test_plan():
    tree = board(
        zone("GND", "In1.Cu", 0, 0, 40, 20),
        fp("J1", 2, 10, [("1", 0, 0, "USB_P"), ("2", 0, 1, "USB_N"), ("3", 0, 2, "GND")],
           "USB_C", lib="Connector:USB_C"),
        fp("D1", 20, 10, [("1", 0, 0, "USB_P"), ("2", 1, 0, "GND")], "PESD5V0"),
        fp("U1", 30, 10, [("1", 0, 0, "USB_P"), ("2", 0, 1, "USB_N"), ("3", 0, 2, "XI")], "MCU"),
        fp("Y1", 15, 3, [("1", 0, 0, "XI"), ("2", 1, 0, "GND")], "8MHz"),
        seg(2, 10, 30, 10, "USB_P"), seg(2, 11, 20, 11, "USB_N"),
        seg(5, 0.4, 35, 0.4, "MCLK"),                  # along the top edge, near J1? no
        seg(5, 15, 25, 15, "SCLK"), seg(5, 15.5, 25, 15.5, "DATA"),   # 0.25 gap < 3H
        seg(3, 12, 8, 12, "BCLK"))                     # passes the connector
    r = er.analyze(tree, ("io",))
    f = by_rule(r)
    esd = {x["net"]: x for x in f["EM-IO-ESD"]}
    assert "USB_N" in esd and esd["USB_N"]["severity"] == "warning"
    assert f["EM-IO-ESD-DIST"][0]["net"] == "USB_P"      # D1 18 mm from the pin
    assert [x["nets"] for x in f["EM-IO-FILTER"]] == [["USB_P", "USB_N"]]   # one per connector
    assert any(x["net"] == "MCLK" and x["severity"] == "warning" for x in f["EM-EDGE-TRACE"])
    assert any(x["net"] == "BCLK" for x in f["EM-CLK-CONN"])
    assert f["EM-XTAL-DIST"][0]["ref"] == "Y1"
    pair = r["io"]["diff_pairs"][0]
    assert (pair["p_mm"], pair["n_mm"]) == (28.0, 18.0) and "EM-DIFF-SKEW" in f
    assert any({x["a"], x["b"]} == {"SCLK", "DATA"} for x in r["io"]["parallel_3h"])
    plan = r["test_plan"]
    assert plan["standard"] == "fcc-class-b" and plan["radiated_limits"][0]["dbuv_m"] == 40.0
    assert {"point": "J1", "why": "connector / cable exit (common-mode current clamp)"} \
        in plan["near_field_probe_points"]
    assert plan["top_risks"] and len(plan["tests"]) == 3


def test_test_plan_switching_bands():
    tree, nl, power = _sw_case()
    r = er.analyze(tree, ("switching",), power=power, nl=nl, fsw={"U1": 1e6}, iout={"U1": 1})
    band = r["test_plan"]["switching_harmonics"][0]
    assert band["source"] == "U1" and band["conducted_150k_30M"][:3] == [1000000, 2000000, 3000000]
    assert band["radiated_from"] == 30000000
    assert {"point": "SW", "why": "U1 switch node"} in r["test_plan"]["near_field_probe_points"]


# ---- diff-pair reference, shielding, thermal ------------------------------------------ #


def test_diff_pair_reference_and_layers():
    items = [zone("GND", "In1.Cu", 0, 0, 20, 20), zone("+5V", "In1.Cu", 20, 0, 30, 20),
             seg(2, 5, 36, 5, "USB_P"), seg(2, 6, 18, 6, "USB_N"),
             seg(18, 6, 36, 6, "USB_N", layer="B.Cu")]
    r = er.analyze(board(*items), ("io",))
    pair = r["io"]["diff_pairs"][0]
    assert pair["layers"] == ["F.Cu", "B.Cu"]
    assert pair["reference"]["nets"] == ["+5V", "GND"] and pair["reference"]["gap_mm"] > 5
    f = by_rule(r)
    assert "EM-DIFF-LAYERS" in f and "EM-DIFF-REF" in f


def test_shielding_advisory():
    # 10 GHz slot: a 15 mm connector; a 1 GHz switcher harmonic set does not reach it,
    # so feed a source list directly through a fake switching result
    tree = board(fp("J1", 5, 10, [("1", 0, 0, "A"), ("2", 14.4, 0, "B")], "BIG"))
    b = er.load_board(tree)
    findings = []
    res = {"switching": [{"regulator": "X", "harmonics": {"fsw_hz": 1e9},
                          "radiation_estimate": {"worst": {"margin_db": 2.0}}}]}
    out = er.shielding_advisory(b, findings, res)
    ap = out["apertures"][0]
    L = 14.4 + math.hypot(0.6, 0.6)            # pad spread + pad diagonal
    assert ap["aperture_mm"] == pytest.approx(L, abs=0.06)
    assert ap["slot_resonance_hz"] == pytest.approx(er.C0 / (2 * L / 1000), rel=0.01)
    assert out["shielding_needed"] == [{"source": "X", "shielding_db": 4.0}]
    assert {f["rule"] for f in findings} == {"EM-SHIELD-SE"}     # 1 GHz far from 10 GHz


def test_thermal_group():
    cap = ('(footprint "Capacitor_SMD:C_0805" (layer "F.Cu") (at 10 10) '
           '(property "Reference" "C1") (property "Value" "10u") (property "Voltage" "25V") '
           '(property "Dielectric" "X5R") (property "Size" "0805") '
           '(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net "VIN")) '
           '(pad "2" smd rect (at 1 0) (size 0.6 0.6) (layers "F.Cu") (net "GND")))')
    c0g = cap.replace('"C1"', '"C2"').replace("X5R", "C0G").replace("at 10 10", "at 10 14")
    tree = board(cap, c0g,
                 fp("U1", 14, 10, [("1", 0, 0, "VIN"), ("2", 0, 1, "GND")], "BUCK"),
                 fp("FB1", 18, 10, [("1", 0, 0, "VIN"), ("2", 1, 0, "VIN_F")], "BLM18"),
                 fp("FB2", 38, 18, [("1", 0, 0, "VIN"), ("2", 1, 0, "VIN_G")], "BLM18"))
    power = {"regulators": [{"name": "U1", "ref": "U1", "topology": "switching"}],
             "rail_voltages": {"VIN": {"volts": 20.0}}}
    r = er.analyze(tree, ("thermal",), power=power, plan=False)
    th = r["thermal"]
    assert [c["ref"] for c in th["dc_bias"]] == ["C1"]           # C0G is not class II
    assert th["dc_bias"][0]["bias_ratio"] == 0.8
    f = by_rule(r)
    assert "read the MPN's DC-bias curve" in f["EM-TH-BIAS"][0]["message"]
    assert [x["ref"] for x in f["EM-TH-FERRITE"]] == ["FB1"]


def test_decoupling_on_a_board_without_stackup(tmp_path):
    # a board saved without (stackup): the mounting-inductance estimate is skipped, not a crash
    p = tmp_path / "n.kicad_pcb"
    p.write_text(f'(kicad_pcb (version 20260206) {LAYERS} '
                 '(gr_rect (start 0 0) (end 40 20) (layer "Edge.Cuts")) '
                 + zone("GND", "In1.Cu", 0, 0, 40, 20) + " "
                 + fp("C1", 11, 11, [("1", 0, 0, "+3V3"), ("2", 1, 0, "GND")], "100n") + " "
                 + via(12.2, 11, "GND") + ")", encoding="utf-8")
    r = er.analyze(sch_io.parse_file(p), ("decoupling",), plan=False)
    bank = r["decoupling"]["banks"]["+3V3"]
    assert "L missing" in bank["z_profile_basis"]
    assert set(bank["z_profile_ohm"].values()) == {None} and bank["z_capacitive_ohm"]["100000"]


def test_net_classes_by_token():
    # OP-20 #22: active-low names are no diff pairs, USB CC no data line,
    # a rail name with a control token is a signal (kicad-happy KH-518)
    nets = frozenset({"/LVDS_P", "/LVDS_N"})
    fast = {n: er.is_fast_net(n, nets) for n in (
        "RESET_N", "/CS_N", "/USB_CC_2", "NEWS", "/PROG_SWCLK", "/USB_D-", "/LVDS_P", "METHOD")}
    assert fast == {"RESET_N": False, "/CS_N": False, "/USB_CC_2": False, "NEWS": False,
                    "/PROG_SWCLK": True, "/USB_D-": True, "/LVDS_P": True, "METHOD": False}
    power = {n: er.is_power_net(n) for n in ("VBUS_FET_EN", "VIN_SENSE", "/PG_20V", "VBUS_MAX",
                                             "+3V3", "/VBUS_+20V", "VCC_3V3_PERIPH")}
    assert power == {"VBUS_FET_EN": False, "VIN_SENSE": False, "/PG_20V": False,
                     "VBUS_MAX": False, "+3V3": True, "/VBUS_+20V": True,
                     "VCC_3V3_PERIPH": True}


def test_z_profile_levels():
    # OP-20 #23-24: full sum only with ESR and L for every cap
    full = er._z_profile([("C1", 1e-7, 0.01, 5e-10)], {"C1": 5e-10})
    f_res = 1 / (2 * math.pi * math.sqrt(1e-9 * 1e-7))
    assert full["z_profile_ohm"]["1e+08"] is not None and "full" in full["z_profile_basis"]
    # at resonance only the ESR is left
    at = er._z_profile([("C1", 1e-7, 0.01, 1e-9)], {})
    assert at["lowest_resonance_hz"] == round(f_res)
    no_esr = er._z_profile([("C1", 1e-7, None, 1e-9)], {})
    assert no_esr["z_profile_ohm"]["100000"] == no_esr["z_capacitive_ohm"]["100000"]
    assert no_esr["z_profile_ohm"]["1e+07"] is None            # 10 MHz > 0.3 x 15.9 MHz


def test_mounting_inductance_counts_the_supply_side():
    # OP-20 #25: cap with a GND via and a +3V3 via into a +3V3 plane on In2
    from kicad_claude.adapters import spice_auto
    tree = board(zone("GND", "In1.Cu", 0, 0, 40, 20), zone("+3V3", "In2.Cu", 0, 0, 40, 20),
                 fp("C1", 10, 10, [("1", 0, 0, "+3V3"), ("2", 1, 0, "GND")], "100n"),
                 via(11.6, 10, "GND"), via(9.4, 10, "+3V3"))
    m = spice_auto.mounting_inductance(tree, ["C1"])["C1"]
    assert m["supply_side"]["plane"] == "In2.Cu" and not m["sides_missing"]
    assert m["l_h"] == pytest.approx(m["ground_side"]["l_h"] + m["supply_side"]["l_h"])
    tree = board(zone("GND", "In1.Cu", 0, 0, 40, 20),
                 fp("C1", 10, 10, [("1", 0, 0, "+3V3"), ("2", 1, 0, "GND")], "100n"),
                 via(11.6, 10, "GND"))
    m = spice_auto.mounting_inductance(tree, ["C1"])["C1"]
    assert m["supply_side"] is None and "supply (+3V3)" in m["sides_missing"][0]


def test_fsw_range_from_the_datasheet():
    # OP-20 #45: the strap uses the typical frequency and carries the min / max
    tree = board(zone("GND", "In1.Cu", 0, 0, 40, 20),
                 fp("U2", 10, 10, [("1", 0, 0, "VIN"), ("2", 0, 1, "SW"), ("5", 0, 2, "RTN")], "BUCK"),
                 fp("R28", 14, 12, [("1", 0, 0, "RTN"), ("2", 1, 0, "GND")], "46.4k"))
    nl = _nl([("U2", "1", "VIN", "power_in", "VIN"), ("U2", "2", "SW", "output", "SW"),
              ("U2", "5", "RT", "passive", "RTN"), ("R28", "1", "", "passive", "RTN"),
              ("R28", "2", "", "passive", "GND")])
    nl.components = {"U2": {"value": "BUCK"}, "R28": {"value": "46.4k"}}
    power = {"regulators": [{"topology": "switching", "name": "U2", "ref": "U2", "value": "BUCK",
                             "channel": None,
                             "input_net": "VIN", "output_net": "OUT", "switch_net": "SW",
                             "inductor": None}], "rail_voltages": {}}
    specs = {"U2": {"mpn": "M", "regulator": {"fsw": {"typ": 1.8e6, "min": 1.6e6, "max": 2.0e6,
                                                      "conditions": "RT = 46.4k", "page": 3}}}}
    r = er.analyze(tree, ("switching",), power=power, nl=nl, specs=specs, plan=False)
    sw = r["switching"][0]
    assert sw["fsw_range_hz"] == [1.6e6, 2.0e6] and "R28 46.4k" in sw["fsw_source"]
