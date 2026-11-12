"""Automatic subcircuit simulation (OP-2)."""

from __future__ import annotations

import math
import os
import random
from pathlib import Path

import pytest

from kicad_claude.adapters import power_review as pr
from kicad_claude.adapters import spice_auto as sa


def _comp(ref, value, tol=None, dnp=False, lib="Device:X"):
    f = f'(field (name "Tolerance") "{tol}")' if tol else ""
    d = ' (property (name "dnp"))' if dnp else ""
    a, b = lib.split(":")
    return (f'(comp (ref "{ref}") (value "{value}") (footprint "") '
            f'(libsource (lib "{a}") (part "{b}") (description "")) (fields {f}){d})')


def _net(code, name, *nodes):
    body = " ".join(f'(node (ref "{r}") (pin "{n}") (pinfunction "{fn}_{n}") (pintype "{t}"))'
                    if fn else f'(node (ref "{r}") (pin "{n}") (pintype "{t}"))'
                    for r, n, fn, t in nodes)
    return f'(net (code "{code}") (name "{name}") {body})'


def _nl(tmp_path, comps, nets):
    p = tmp_path / "s.net"
    p.write_text(f'(export (version "E") (components {" ".join(comps)}) (nets {" ".join(nets)}))',
                 encoding="utf-8")
    return pr.parse_netlist(p)


def _board(tmp_path):
    comps = [_comp("U1", "BUCK"), _comp("U2", "LDO"), _comp("U3", "MCU"),
             _comp("R1", "100k", "1%"), _comp("R2", "20k", "1%"),     # FB divider
             _comp("R3", "10k"), _comp("C3", "100n"),                 # RC into U3.EN
             _comp("L1", "10u", "20%"), _comp("C1", "10u", "10%"), _comp("C2", "10u", "10%"),
             _comp("R4", "1"), _comp("C4", "22u"), _comp("C5", "22u", dnp=True),   # damping
             _comp("L2", "4u7"),                                       # switch-node inductor
             _comp("J1", "IN")]
    nets = [
        _net(1, "VIN", ("J1", "1", "", "passive"), ("U1", "1", "VIN", "power_in")),
        _net(2, "GND", ("J1", "2", "", "passive"), ("R2", "2", "", "passive"),
             ("C3", "2", "", "passive"), ("C1", "2", "", "passive"), ("C2", "2", "", "passive"),
             ("C4", "2", "", "passive"), ("C5", "2", "", "passive"),
             ("U1", "9", "GND", "power_in")),
        _net(3, "SW", ("U1", "2", "SW", "output"), ("L2", "1", "", "passive")),
        _net(4, "+5V", ("L2", "2", "", "passive"), ("R1", "1", "", "passive"),
             ("C1", "1", "", "passive"), ("L1", "1", "", "passive"), ("R3", "1", "", "passive")),
        _net(5, "FB", ("U1", "3", "FB", "input"), ("R1", "2", "", "passive"),
             ("R2", "1", "", "passive")),
        _net(6, "EN_RC", ("R3", "2", "", "passive"), ("C3", "1", "", "passive"),
             ("U3", "1", "EN", "input")),
        _net(7, "+5V_F", ("L1", "2", "", "passive"), ("C2", "1", "", "passive"),
             ("R4", "1", "", "passive"), ("U2", "1", "IN", "power_in")),
        _net(8, "DAMP", ("R4", "2", "", "passive"), ("C4", "1", "", "passive"),
             ("C5", "1", "", "passive")),
    ]
    return _nl(tmp_path, comps, nets)


REGS = [{"ref": "U1", "topology": "switching", "switch_net": "SW", "input_net": "VIN",
         "output_net": "+5V"}]


def test_divider_with_vref(tmp_path):
    nl = _board(tmp_path)
    vref = {"FB": {"min": 0.784, "typ": 0.8, "max": 0.816, "source": "datasheet X p.6"}}
    r = sa.analyze(nl, ("divider",), volts={"+5V": 4.8}, vref=vref, mc=2000)
    d = next(x for x in r["dividers"] if x["mid"] == "FB")
    assert d["ratio"] == pytest.approx(20 / 120, abs=1e-6)
    assert d["v_mid"] == pytest.approx(0.8, abs=1e-4)
    v = d["vout"]
    assert v["typ"] == pytest.approx(4.8)
    lo = 0.784 * (99e3 + 20.2e3) / 20.2e3
    hi = 0.816 * (101e3 + 19.8e3) / 19.8e3
    assert v["worst"] == [pytest.approx(lo, abs=1e-3), pytest.approx(hi, abs=1e-3)]
    mc = v["monte_carlo"]
    assert lo - 1e-3 <= mc["p0_1"] <= mc["median"] <= mc["p99_9"] <= hi + 1e-3
    assert any(f["rule"] == "SP-DIV-VOUT" for f in r["findings"])


def test_tolerance_parse_and_missing():
    assert sa.tolerance({"fields": {"Tolerance": "0.1 %"}}) == pytest.approx(0.001)
    assert sa.tolerance({"fields": {}}) is None


def test_rc_and_ladder_detection(tmp_path):
    nl = _board(tmp_path)
    r = sa.analyze(nl, ("rc", "divider"), regulators=REGS, volts={"+5V": 5.0},
                   sources={"+5V": "regulator U1", "+5V_F": "via L1 from +5V"})
    rc = r["rc_filters"]
    assert [x["node"] for x in rc] == ["EN_RC"]                # rails are not RC filters
    assert rc[0]["fc_hz"] == pytest.approx(1 / (2 * math.pi * 1e4 * 1e-7), rel=1e-3)
    tol = [f for f in r["findings"] if f["rule"] == "SP-TOL"]
    assert all("R3" not in f["message"] for f in tol)          # R3 is no divider

    lads = sa.find_ladders(nl, {"SW"}, {"+5V": "regulator U1", "+5V_F": "via L1 from +5V"})
    assert len(lads) == 1                                      # L2 on the switch node excluded
    lad = lads[0]
    assert lad["input"] == "+5V" and lad["links"] == ["L1"]
    damp = lad["shunts"]["+5V_F"]["damping"]
    assert damp == [{"r": "R4", "node": "DAMP", "caps": ["C4"]}]   # DNP C5 ignored
    deck, node, _ = sa.ladder_deck(nl, lad, 10, 1e8, loads={"+5V_F": 10}, source_ohm=0.1)
    assert "RSRC src" in deck and f"RLOAD_{node['+5V_F']}" in deck
    assert f"LL1 {node['+5V']} {node['+5V_F']} 1e-05" in deck


@pytest.mark.skipif(sa.find_ngspice() is None, reason="ngspice not on PATH")
def test_ladder_ac_sweep(tmp_path):
    nl = _board(tmp_path)
    r = sa.analyze(nl, ("lc",), regulators=REGS, freqs=[1e6],
                   sources={"+5V": "regulator U1", "+5V_F": "via L1 from +5V"}, mc=5)
    lad = r["ladders"][0]
    assert lad["output"] == "+5V_F"
    # 10 uH into 10 uF (+ damping 1 ohm / 22 uF): far above resonance ~ -40 dB/dec
    att = lad["response"]["at"]["1e+06"]
    assert -100 < att < -60
    assert lad["response"]["peaks"]
    assert lad["monte_carlo"]["peaking_db_max"] is not None
    sev = {f["rule"]: f["severity"] for f in r["findings"]}
    assert sev.get("SP-LC-PEAK", "info") == "info"           # open load -> info only
    # loaded: a warning when the peak stays above the limit
    r = sa.analyze(nl, ("lc",), regulators=REGS, peaking_warn_db=0.1,
                   sources={"+5V": "regulator U1"}, loads={"+5V_F": 1000})
    assert {f["rule"]: f["severity"] for f in r["findings"]}["SP-LC-PEAK"] == "warning"


def test_no_ngspice(tmp_path):
    nl = _board(tmp_path)
    lad = sa.find_ladders(nl, {"SW"}, {"+5V": "regulator U1"})[0]
    rec = sa.analyze_ladder(nl, lad, [1e6], None, 0, random.Random(1), {})
    assert rec["error"] == sa.NOSIM and "deck" in rec


# ---- op-amp stages, crystals, parasitics, spec tolerance ------------------------ #


def _opamp_nl(tmp_path):
    comps = [_comp("U1", "OPA1612"), _comp("R1", "10k"), _comp("R2", "1k"), _comp("C1", "100p"),
             _comp("R3", "20k"), _comp("R4", "10k"), _comp("Y1", "24MHz"),
             _comp("C5", "18p"), _comp("C6", "22p"), _comp("U2", "MCU")]
    nets = [
        _net(1, "IN_A", ("U1", "3", "+INA", "input")),
        _net(2, "FB_A", ("U1", "2", "-INA", "input"), ("R1", "2", "", "passive"),
             ("R2", "1", "", "passive"), ("C1", "2", "", "passive")),
        _net(3, "OUT_A", ("U1", "1", "OUTA", "output"), ("R1", "1", "", "passive"),
             ("C1", "1", "", "passive")),
        _net(4, "GND", ("R2", "2", "", "passive"), ("U1", "5", "+INB", "input"),
             ("C5", "2", "", "passive"), ("C6", "2", "", "passive")),
        _net(5, "IN_B", ("R4", "1", "", "passive")),
        _net(6, "FB_B", ("U1", "6", "-INB", "input"), ("R3", "2", "", "passive"),
             ("R4", "2", "", "passive")),
        _net(7, "OUT_B", ("U1", "7", "OUTB", "output"), ("R3", "1", "", "passive")),
        _net(8, "XI", ("Y1", "1", "", "passive"), ("C5", "1", "", "passive"),
             ("U2", "1", "XIN", "input")),
        _net(9, "XO", ("Y1", "2", "", "passive"), ("C6", "1", "", "passive"),
             ("U2", "2", "XOUT", "output")),
    ]
    return _nl(tmp_path, comps, nets)


def test_opamp_stages(tmp_path):
    nl = _opamp_nl(tmp_path)
    specs = {"U1": {"electrical": {"GBW": {"typ": 80e6, "page": 5}}}}
    r = sa.analyze(nl, ("opamp",), specs=specs)
    st = {s["channel"]: s for s in r["opamp_stages"]}
    a, b = st["A"], st["B"]
    assert a["topology"] == "non_inverting" and a["gain"] == pytest.approx(11.0)
    assert a["bandwidth_hz"] == pytest.approx(80e6 / 11, rel=1e-3)
    assert a["feedback_pole_hz"] == pytest.approx(1 / (2 * math.pi * 1e4 * 1e-10), rel=1e-3)
    assert b["topology"] == "inverting" and b["gain"] == pytest.approx(-2.0)
    assert b["noise_gain"] == pytest.approx(3.0) and b["input"] == "IN_B"
    assert sum(f["rule"] == "SP-OPAMP" for f in r["findings"]) == 2


def test_crystal_load(tmp_path):
    nl = _opamp_nl(tmp_path)
    r = sa.analyze(nl, ("crystal",), c_stray_pf=3.0)
    x = r["crystals"][0]
    assert x["c_series_pf"] == pytest.approx(18 * 22 / 40, abs=1e-3)
    assert x["cl_effective_pf"] == pytest.approx(18 * 22 / 40 + 3, abs=1e-3)
    assert r["findings"][0]["severity"] == "info"              # CL not in the cache
    specs = {"Y1": {"electrical": {"CL": {"typ": 18e-12, "page": 2}}}}
    r = sa.analyze(nl, ("crystal",), specs=specs, c_stray_pf=3.0)
    f = r["findings"][0]
    assert f["rule"] == "SP-XTAL-CL" and f["severity"] == "warning"   # 12.9 vs 18 pF


def test_via_inductance_and_spec_tolerance():
    assert sa.via_inductance_h(1.6, 0.3) * 1e9 == pytest.approx(1.299, abs=1e-3)
    assert sa.via_inductance_h(0, 0.3) == 0.0
    specs = {"L1": {"electrical": {"L": {"min": 1.76e-6, "typ": 2.2e-6, "max": 2.64e-6}}}}
    assert sa.spec_tolerance(specs, "L1") == pytest.approx(0.2)
    assert sa.spec_tolerance({}, "L1") is None


def test_deck_mounting_and_esl(tmp_path):
    nl = _board(tmp_path)
    lad = sa.find_ladders(nl, {"SW"}, {"+5V": "regulator U1"})[0]
    specs = {"C2": {"electrical": {"ESR": {"typ": 0.005}, "ESL": {"typ": 5e-10}}}}
    deck, node, _ = sa.ladder_deck(nl, lad, 10, 1e8, specs=specs, mount_l={"C2": 1e-10})
    assert "RPC2_R" in deck and "LPC2_L" in deck and "6e-10" in deck
    assert "LPC1_L" not in deck


def test_mounting_inductance_board(tmp_path):
    from kicad_claude.adapters import sch_io
    text = ('(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) '
            '(2 "B.Cu" signal)) (setup (stackup (layer "F.Cu" (type "copper")) '
            '(layer "dielectric 1" (type "prepreg") (thickness 0.2)) (layer "In1.Cu" (type "copper")) '
            '(layer "dielectric 2" (type "core") (thickness 1.2)) (layer "B.Cu" (type "copper")))) '
            '(gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts")) '
            '(zone (net "GND") (layer "In1.Cu") (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20))) '
            '(filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))) '
            '(footprint "C" (layer "F.Cu") (at 10 10) (property "Reference" "C1") '
            '(pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net "VCC")) '
            '(pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net "GND"))) '
            '(via (at 11.5 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND")))')
    p = tmp_path / "m.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    m = sa.mounting_inductance(sch_io.parse_file(p), ["C1"])["C1"]
    assert m["plane"] == "In1.Cu" and m["height_mm"] == 0.2 and m["pad_to_via_mm"] == 1.0
    assert m["l_via_h"] == pytest.approx(sa.via_inductance_h(0.2, 0.3))
    assert m["connection"].startswith("pour") and m["l_trace_h"] > 0
    assert m["l_h"] == pytest.approx(m["l_via_h"] + m["l_trace_h"])


# ---- vendor op-amp model, strip inductance ------------------------------------- #

OPAMP_LIB = """* test op-amp: A0 = 1e5, dominant pole 10 Hz -> GBW 1 MHz
.subckt OPTEST INP INN VCC VEE OUT
E1 int 0 INP INN 1e5
R1 int p1 1k
C1 p1 0 15.9155u
E2 OUT 0 p1 0 1
RIN INP INN 10Meg
.ends OPTEST
"""


@pytest.mark.skipif(sa.find_ngspice() is None, reason="ngspice not on PATH")
def test_opamp_vendor_model(tmp_path):
    lib = tmp_path / "optest.lib"
    lib.write_text(OPAMP_LIB)
    nl = _opamp_nl(tmp_path)
    models = {"U1": {"lib": str(lib)}}                       # roles from node names
    r = sa.analyze(nl, ("opamp",), opamp_models=models,
                   supplies={"U1": {"V+": 15, "V-": -15}})
    a = next(s for s in r["opamp_stages"] if s["channel"] == "A")
    sim = a["simulated"]
    assert a["model"] == "vendor: OPTEST"
    assert sim["gain_lf"] == pytest.approx(11.0, rel=0.01)
    assert sim["f_3db_hz"] < 1e6 / 11                              # Cf pole adds to GBW / NG
    assert sim["roles"] == {"INP": "+IN", "INN": "-IN", "VCC": "V+", "VEE": "V-", "OUT": "OUT"}
    b = next(s for s in r["opamp_stages"] if s["channel"] == "B")
    assert b["simulated"]["gain_lf"] == pytest.approx(2.0, rel=0.01)   # |inverting gain|
    assert b["simulated"]["f_3db_hz"] == pytest.approx(1e6 / 3, rel=0.15)   # GBW / noise gain
    assert "vendor: OPTEST" in next(f["message"] for f in r["findings"] if f["ref"] == "U1")


def test_opamp_model_roles_and_errors(tmp_path):
    lib = tmp_path / "x.lib"
    lib.write_text(".subckt A 1 2 3 4 5\n+ PARAMS: X=1\nR1 1 2 1\n.ends\n")
    name, nodes = sa.parse_subckt(str(lib))
    assert (name, nodes) == ("A", ["1", "2", "3", "4", "5"])
    assert sa.model_roles(nodes, ["+IN", "-IN", "V+", "V-", "OUT"])[2] == "V+"
    with pytest.raises(ValueError):
        sa.model_roles(nodes, ["+IN"])
    nl = _opamp_nl(tmp_path)
    nl.components["U1"]["fields"].update({"Sim.Library": "m.lib", "Sim.Name": "OPA",
                                          "Sim.Pins": "1=OUT 2=IN- 3=IN+"})
    m = sa.model_from_fields(nl, "U1")
    assert m["subckt"] == "OPA" and m["sim_pins"] == {"1": "OUT", "2": "IN-", "3": "IN+"}
    r = sa.analyze(nl, ("opamp",), sim="")                         # no simulator
    assert all("ideal" in s.get("model", "") or sa.NOSIM in s.get("model", "")
               for s in r["opamp_stages"])


def test_strip_inductance():
    # w/h = 1: (mu0/2pi) ln(8.25) per metre
    assert sa.strip_inductance_h(1.0, 0.2, 0.2) == pytest.approx(2e-7 * math.log(8.25) * 1e-3)
    wide = sa.strip_inductance_h(1.0, 2.0, 0.2)
    assert wide < sa.strip_inductance_h(1.0, 0.2, 0.2) and wide > 0
    assert sa.strip_inductance_h(0, 1, 1) == 0.0


# ---- simulator backends (ngspice / LTspice) ---------------------------------- #

FIXTURES = Path(__file__).parent / "fixtures"


def test_ltspice_raw_reader():
    from kicad_claude.adapters import spice_backends as sb

    raw = sb.read_raw(FIXTURES / "ltspice_rc_ac.raw")       # 1k / 159.155n RC, 10 Hz-100 kHz
    assert raw["plot"] == "AC Analysis" and "complex" in raw["flags"]
    assert raw["variables"][:3] == ["frequency", "V(n0)", "V(n1)"]
    f = [z.real for z in raw["data"]["frequency"]]
    v = raw["data"]["V(n1)"]
    assert len(f) == 21 and f[0] == pytest.approx(10)
    i = min(range(len(f)), key=lambda k: abs(f[k] - 1000))   # corner 1 kHz: -3 dB
    assert 20 * math.log10(abs(v[i])) == pytest.approx(-3.01, abs=0.05)


def test_simulator_discovery_order(tmp_path, monkeypatch):
    from kicad_claude.adapters import spice_backends as sb

    env = tmp_path / "env" / "LTspice.exe"
    onpath = tmp_path / "bin" / "LTspice.exe"
    for p in (env, onpath):
        p.parent.mkdir()
        p.write_bytes(b"")
    monkeypatch.setenv("LTSPICE_PATH", str(env))
    monkeypatch.setenv("PATH", f".{os.pathsep}{onpath.parent}")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))
    assert sb.find_ltspice() == str(env)
    monkeypatch.delenv("LTSPICE_PATH")
    assert sb.find_ltspice() == str(onpath)
    # a relative PATH entry is never searched, even with an LTspice.exe in the cwd
    monkeypatch.setenv("PATH", ".")
    monkeypatch.chdir(onpath.parent)
    cands = [str(p) for p in sb._ltspice_candidates()]
    assert not any(c.startswith(".") or c == "LTspice.exe" for c in cands)
    monkeypatch.setattr(sb, "find_ngspice", lambda: "/x/ngspice")
    monkeypatch.setattr(sb, "find_ltspice", lambda: "/y/LTspice.exe")
    assert sb.detect("auto").name == "ngspice" and sb.detect("ltspice").name == "ltspice"
    monkeypatch.setattr(sb, "find_ngspice", lambda: None)
    assert sb.detect("auto").name == "ltspice" and sb.detect("ngspice") is None
    with pytest.raises(ValueError):
        sb.detect("xyce")


@pytest.mark.slow
def test_ltspice_matches_ngspice():
    from kicad_claude.adapters import spice_backends as sb

    lt, ng = sb.find_ltspice(), sb.find_ngspice()
    if not lt or not ng:
        pytest.skip("needs both LTspice and ngspice")
    deck = (FIXTURES / "ltspice_rc_ac.cir").read_text(encoding="utf-8")
    a = sb.LtspiceBackend(lt).run_ac(deck, ["n1"])["n1"]
    b = sb.NgspiceBackend(ng).run_ac(deck, ["n1"])["n1"]
    for f, db in a:
        ref = min(b, key=lambda x: abs(math.log10(x[0] / f)))
        assert db == pytest.approx(ref[1], abs=0.5), f


# ---- behavioural models from datasheet specs ---------------------------------- #

OPX = {"mpn": "OPX1", "category": "opamp",
       "electrical": {"GBW": {"typ": 1e6, "unit": "Hz", "page": 5},
                      "Aol": {"typ": 120, "unit": "dB", "page": 6}}}


def test_behavioural_models_need_their_keys():
    from kicad_claude.adapters import spice_models as sm

    gen = sm.opamp(OPX)
    assert gen["subckt"] == "OPAMP_OPX1" and gen["source"] == "behavioural, from datasheet OPX1 p.5"
    assert gen["aol"] == pytest.approx(1e6) and ".subckt OPAMP_OPX1 inp inn out vcc vee" in gen["text"]
    assert sm.opamp({"mpn": "X", "electrical": {"GBW": {"typ": 1e6, "page": 1}}})["missing"] == \
        ["electrical.Aol (dB or V/V)"]
    ldo = {"mpn": "LDO1", "regulator": {"vref": {"typ": 1.2, "page": 3},
                                        "dropout": {"max": 0.3, "page": 4}},
           "electrical": {"IQ": {"typ": 5e-5, "page": 4}}}
    g = sm.ldo(ldo)
    assert g["nodes"] == ["vin", "vout", "gnd", "fb"] and "min(V(vin, gnd) - 0.3" in g["text"]
    assert sm.ldo({"mpn": "L", "regulator": {"vref": {"typ": 1.2, "page": 3}}})["missing"] == \
        ["regulator.dropout"]
    assert sm.vref({"mpn": "R", "regulator": {"vref": {"typ": 2.5, "page": 2}}})["nodes"] == \
        ["out", "gnd"]


def test_opamp_ideal_names_missing_keys(tmp_path):
    nl = _opamp_nl(tmp_path)
    specs = {"U1": {"mpn": "OPY", "electrical": {"GBW": {"typ": 1e6, "unit": "Hz", "page": 5}}}}
    r = sa.analyze(nl, ("opamp",), specs=specs, sim="")
    a = next(s for s in r["opamp_stages"] if s["channel"] == "A")
    assert a["model"].startswith("ideal") and "electrical.Aol" in a["model"]


@pytest.mark.slow
def test_behavioural_opamp_bandwidth(tmp_path):
    from kicad_claude.adapters import spice_backends as sb

    sim = sb.detect("auto")
    if sim is None:
        pytest.skip("no SPICE simulator")
    nl = _opamp_nl(tmp_path)
    r = sa.analyze(nl, ("opamp",), specs={"U1": OPX}, supplies={"U1": {"V+": 15, "V-": -15}},
                   sim=sim)
    b = next(s for s in r["opamp_stages"] if s["channel"] == "B")     # inverting, NG 3
    assert b["model"] == "behavioural, from datasheet OPX1 p.5: OPAMP_OPX1"
    assert b["simulated"]["gain_lf"] == pytest.approx(2.0, rel=0.01)
    assert b["simulated"]["f_3db_hz"] == pytest.approx(1e6 / 3, rel=0.15)    # GBW / NG


def test_opamp_with_bare_sign_pins_and_unnamed_output(tmp_path):
    # MLAB-style symbol (AIRDOS05): pins "+", "-", and an unnamed output pin
    comps = [_comp("U1", "AD8691"), _comp("R4", "10k"), _comp("R5", "100k")]
    nets = [_net(1, "REF", ("U1", "3", "+", "input")),
            _net(2, "INV", ("U1", "4", "-", "input"), ("R4", "1", "", "passive"),
                 ("R5", "1", "", "passive")),
            _net(3, "OUT", ("U1", "1", "", "output"), ("R5", "2", "", "passive")),
            _net(4, "IN", ("R4", "2", "", "passive")),
            _net(5, "VCC5", ("U1", "5", "V+", "power_in")),
            _net(6, "GNDA", ("U1", "2", "V-", "power_in"))]
    st = sa.find_opamp_stages(_nl(tmp_path, comps, nets), {})
    assert [(s["ref"], s["topology"], s["gain"]) for s in st] == [("U1", "inverting", -10.0)]
    from kicad_claude.adapters import circuit_review
    assert circuit_review.opamp_channels(_nl(tmp_path, comps, nets), {})["U1"][""]["out"] == "OUT"


def test_rc_highpass_dc_block():
    # OP-18: rail -> 100n -> SMA with 10k bleed (firbox C61 / R50): fc 159 Hz;
    # a feed-forward cap on an FB node and a node also fed by a fitted R are not
    from kicad_claude.adapters.power_review import Netlist, Pin

    nl = Netlist()

    def add(ref, num, name, typ, net, value=None):
        p = Pin(ref, num, name, typ, net)
        nl.pins[ref].append(p)
        nl.nets.setdefault(net, []).append(p)
        if value:
            nl.components[ref] = {"value": value}
    add("C61", "1", "", "passive", "+20V", "100n")
    add("C61", "2", "", "passive", "SMA", "100n")
    add("R50", "1", "", "passive", "SMA", "10k")
    add("R50", "2", "", "passive", "GND", "10k")
    add("J5", "1", "In", "passive", "SMA", "SMA")
    add("C37", "1", "", "passive", "-18V", "10n")
    add("C37", "2", "", "passive", "FBN", "10n")
    add("R34", "1", "", "passive", "FBN", "10k")
    add("R34", "2", "", "passive", "GND", "10k")
    add("U5", "5", "FB", "input", "FBN", "LDO")
    hp = sa.find_rc_highpass(nl, {"+20V", "-18V", "SMA"})
    assert [(h["series_cap"], h["shunt_r"], h["fc_hz"]) for h in hp] == [("C61", ["R50"], 159.155)]
    assert "J5 goes off-board" in hp[0]["load"]
    # a 50 ohm instrument on the SMA sets the corner: 1 / (2 pi (10k || 50) 100n)
    hp = sa.find_rc_highpass(nl, {"+20V", "-18V", "SMA"}, loads={"SMA": 50})
    assert hp[0]["fc_hz"] == pytest.approx(1 / (2 * math.pi * (1 / (1e-4 + 0.02)) * 1e-7), rel=1e-4)
    add("R49", "1", "", "passive", "+20V", "0R")      # a fitted DC path: no longer a high-pass
    add("R49", "2", "", "passive", "SMA", "0R")
    assert sa.find_rc_highpass(nl, {"+20V", "-18V", "SMA"}) == []
