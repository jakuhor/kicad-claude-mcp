"""Release tools: Gerber analysis, design diff, what-if, lifecycle (OP-3)."""

from __future__ import annotations

import json
import os
import time

import pytest

from kicad_claude.adapters import design_diff as dd
from kicad_claude.adapters import gerber_review as gr
from kicad_claude.adapters import power_review as pr
from kicad_claude.adapters import project_settings as ps

COPPER = """%TF.FileFunction,Copper,L1,Top*%
%TF.FilePolarity,Positive*%
%TF.CreationDate,2026-01-01T00:00:00+00:00*%
%FSLAX46Y46*%
%MOMM*%
%TA.AperFunction,Conductor*%
%ADD10C,0.100000*%
%TD*%
%TA.AperFunction,SMDPad,CuDef*%
%ADD11R,1.000000X0.500000*%
%TD*%
D10*
X1000000Y1000000D02*
X5000000Y1000000D01*
D11*
X2000000Y2000000D03*
G36*
X0Y0D02*
X1000000Y0D01*
X1000000Y1000000D01*
G37*
M02*
"""
OUTLINE = """%TF.FileFunction,Profile,NP*%
%FSLAX46Y46*%
%MOMM*%
%ADD10C,0.050000*%
D10*
X0Y0D02*
X50000000Y0D01*
X50000000Y-30000000D01*
X0Y-30000000D01*
X0Y0D01*
M02*
"""
DRILL = """M48
; #@! TF.FileFunction,Plated,1,2,PTH
METRIC
; #@! TA.AperFunction,Plated,PTH,ViaDrill
T1C0.150
; #@! TA.AperFunction,Plated,PTH,ComponentDrill
T2C1.000
%
G90
T1
X10.0Y-10.0
X11.0Y-10.0
T2
X60.0Y-10.0
M30
"""


def _fab(tmp_path, job_layers=2, extra_job=()):
    d = tmp_path / "gerbers"
    d.mkdir()
    (d / "b-F_Cu.gtl").write_text(COPPER)
    (d / "b-B_Cu.gbl").write_text(COPPER.replace("L1,Top", "L2,Bot"))
    (d / "b-Edge_Cuts.gm1").write_text(OUTLINE)
    (d / "b-PTH.drl").write_text(DRILL)
    files = [{"Path": "b-F_Cu.gtl", "FileFunction": "Copper,L1,Top"},
             {"Path": "b-B_Cu.gbl", "FileFunction": "Copper,L2,Bot"},
             {"Path": "b-Edge_Cuts.gm1", "FileFunction": "Profile,NP"}, *extra_job]
    (d / "b-job.gbrjob").write_text(json.dumps({"GeneralSpecs": {
        "Size": {"X": 50.0, "Y": 30.0}, "LayerNumber": job_layers, "BoardThickness": 1.6},
        "FilesAttributes": files}))
    return d


def test_parse_gerber_and_drill(tmp_path):
    d = _fab(tmp_path)
    g = gr.parse_gerber(d / "b-F_Cu.gtl")
    assert g["function"] == "Copper,L1,Top" and g["draws"] == 1 and g["flashes"] == 1
    assert g["regions"] == 1 and g["min_conductor_width_mm"] == 0.1
    x = gr.parse_drill(d / "b-PTH.drl")
    assert x["holes"] == 3 and x["min_drill_mm"] == 0.15
    assert x["tools"]["1"] == {"diameter_mm": 0.15, "function": "Plated,PTH,ViaDrill", "hits": 2}


def test_analyze_gerbers(tmp_path):
    d = _fab(tmp_path, job_layers=4, extra_job=[
        {"Path": "b-F_Mask.gts", "FileFunction": "Soldermask,Top"},
        {"Path": "b-User.gbr", "FileFunction": "Other,User"}])
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    os.utime(board, (time.time(), time.time()))
    r = gr.analyze(d, ps.FAB_PRESETS["jlcpcb_4l"]["rules"], board)
    rules = {(f["rule"], f["severity"]) for f in r["findings"]}
    assert ("GB-LAYERS", "error") in rules                    # job 4, folder 2
    assert ("GB-MISSING-FILE", "error") in rules              # soldermask listed, missing
    assert ("GB-MISSING-FILE", "info") in rules               # user layer, not fab
    assert ("GB-DRILL-OUTSIDE", "error") in rules             # hole at x=60 > 50
    assert ("GB-MIN-TRACE", "error") in rules                 # 0.1 < 0.127
    assert ("GB-MIN-DRILL", "error") in rules                 # 0.15 < 0.2
    assert ("GB-STALE", "warning") in rules                   # created 2026-01-01
    assert r["copper_layers"] == 2 and r["holes"] == 3
    assert not any(f["rule"] == "GB-SIZE" for f in r["findings"])


def _nl(tmp_path, name, comps, nets):
    body = " ".join(f'(comp (ref "{r}") (value "{v}") (footprint "{fp}") '
                    f'(libsource (lib "Device") (part "X") (description "")) '
                    f'(fields (field (name "MPN") "{mpn}")))' for r, v, fp, mpn in comps)
    nb = " ".join(f'(net (code "{i}") (name "{n}") '
                  + " ".join(f'(node (ref "{r}") (pin "{p}") (pintype "passive"))' for r, p in pins)
                  + ")" for i, (n, pins) in enumerate(nets.items(), 1))
    path = tmp_path / f"{name}.net"
    path.write_text(f'(export (version "E") (components {body}) (nets {nb}))', encoding="utf-8")
    return pr.parse_netlist(path)


def test_diff_netlists(tmp_path):
    a = _nl(tmp_path, "a", [("R1", "10k", "R_0603", "A"), ("R2", "1k", "R_0603", "B"),
                            ("C1", "1u", "C_0603", "C")],
            {"VIN": [("R1", "1")], "MID": [("R1", "2"), ("R2", "1")], "GND": [("R2", "2"), ("C1", "2")],
             "OLD": [("C1", "1")]})
    b = _nl(tmp_path, "b", [("R1", "12k", "R_0603", "A"), ("R2", "1k", "R_0805", "B2"),
                            ("C2", "1u", "C_0603", "C")],
            {"VIN": [("R1", "1")], "TAP": [("R1", "2"), ("R2", "1")], "GND": [("R2", "2"), ("C2", "2")],
             "NEW": [("C2", "1")]})
    d = dd.diff_netlists(a, b)
    assert d["added"] == ["C2"] and d["removed"] == ["C1"]
    ch = {c["ref"]: c for c in d["changed"]}
    assert ch["R1"]["value"] == ["10k", "12k"]
    assert ch["R2"]["footprint"] == ["R_0603", "R_0805"] and ch["R2"]["fields"]["MPN"] == ["B", "B2"]
    assert d["net_renames"] == [{"from": "MID", "to": "TAP"}]
    assert d["pin_net_changes"] == []                    # the rename covers R1.2 / R2.1


def _pcb(tmp_path, name, fps, segs):
    items = " ".join(f'(footprint "{lib}" (layer "{side}") (at {x} {y} {rot}) '
                     f'(property "Reference" "{ref}"))' for ref, lib, x, y, rot, side in fps)
    items += " " + " ".join(f'(segment (start {a} 0) (end {b_} 0) (width 0.2) (layer "F.Cu") '
                            f'(net "{n}"))' for a, b_, n in segs)
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text('(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal) (2 "B.Cu" signal)) '
                 '(gr_rect (start 0 0) (end 40 20) (layer "Edge.Cuts")) ' + items + ")")
    from kicad_claude.adapters import sch_io
    return sch_io.parse_file(p)


def test_diff_pcbs(tmp_path):
    a = _pcb(tmp_path, "a", [("R1", "L:R", 10, 10, 0, "F.Cu"), ("R2", "L:R", 20, 10, 0, "F.Cu"),
                             ("U1", "L:U", 5, 5, 0, "F.Cu")], [(0, 10, "SIG")])
    b = _pcb(tmp_path, "b", [("R1", "L:R", 12, 10, 90, "F.Cu"), ("R2", "L:R", 20, 10, 0, "B.Cu"),
                             ("U2", "L:U", 5, 5, 0, "F.Cu")], [(0, 15, "SIG")])
    d = dd.diff_pcbs(a, b)
    assert d["footprints_added"] == ["U2"] and d["footprints_removed"] == ["U1"]
    ch = {c["ref"]: c for c in d["footprints_changed"]}
    assert ch["R1"]["moved_mm"] == 2.0 and ch["R1"]["rotation"] == [0.0, 90.0]
    assert ch["R2"]["side"] == ["F.Cu", "B.Cu"]
    assert d["routing_changed"] == [{"net": "SIG", "length_mm": [10.0, 15.0], "segments": [1, 1]}]


def test_what_if_apply_changes(tmp_path):
    from kicad_claude.tools.review import release as release
    nl = _nl(tmp_path, "w", [("R1", "10k", "", "A"), ("C1", "1u", "", "C")], {"N": [("R1", "1")]})
    applied = release._apply_changes(nl, {"R1": "12k", "C1": {"dnp": True, "fields": {"MPN": "X"}}})
    assert nl.components["R1"]["value"] == "12k"
    assert nl.components["C1"]["dnp"] is True and nl.components["C1"]["fields"]["MPN"] == "X"
    assert len(applied) == 2
    with pytest.raises(ValueError):
        release._apply_changes(nl, {"R9": "1k"})


def test_audit_lifecycle_classification(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    from kicad_claude.tools.review import release as release
    from kicad_claude.tools.sourcing import distributors as sourcing

    nl_path = tmp_path / "l.net"
    comps = [("U1", "OLD", "AAA-EOL"), ("U2", "NR", "BBB-NRND"), ("U3", "RS", "CCC-RESTR"),
             ("U4", "NS", "DDD-NOSTOCK"), ("U5", "OK", "EEE-ONE"), ("U6", "?", "FFF-NONE")]
    body = " ".join(f'(comp (ref "{r}") (value "{v}") (footprint "") (libsource (lib "x") '
                    f'(part "x") (description "")) (fields (field (name "MPN") "{m}")))'
                    for r, v, m in comps)
    nl_path.write_text(f'(export (version "E") (components {body}) (nets))', encoding="utf-8")
    data = {
        ("digikey", "AAA-EOL"): {"lifecycle": "Obsolete", "stock": 10},
        ("mouser", "AAA-EOL"): {"lifecycle": "", "stock": 0},
        ("digikey", "BBB-NRND"): {"lifecycle": "Not For New Designs", "stock": 5},
        ("mouser", "BBB-NRND"): {"lifecycle": "", "stock": 5},
        ("mouser", "CCC-RESTR"): {"lifecycle": "Restricted Availability", "stock": 3,
                                  "suggested_replacement": "CCC-NEW"},
        ("digikey", "CCC-RESTR"): {"lifecycle": "Active", "stock": 3},
        ("digikey", "DDD-NOSTOCK"): {"lifecycle": "Active", "stock": 0},
        ("digikey", "EEE-ONE"): {"lifecycle": "Active", "stock": 0},
        ("mouser", "EEE-ONE"): {"lifecycle": "", "stock": 9},
    }

    def fake(name, mpn, limit=5):
        h = data.get((name, mpn))
        return [{"mpn": mpn, **h}] if h else []

    monkeypatch.setattr(sourcing, "search_source", fake)
    mcp = FastMCP("t")
    release.register(mcp)
    r = mcp._tool_manager.get_tool("audit_lifecycle").fn(netlist_path=str(nl_path))
    got = {(f["rule"], f["ref"]) for f in r["findings"]}
    assert ("LC-EOL", "U1") in got and ("LC-NRND", "U2") in got
    assert ("LC-RESTRICTED", "U3") in got
    assert any("CCC-NEW" in f["message"] for f in r["findings"] if f["rule"] == "LC-RESTRICTED")
    assert ("LC-STOCK", "U4") in got and ("LC-SINGLE", "U5") in got
    assert ("LC-NOTFOUND", "U6") in got
    assert r["counts"]["error"] == 1


def test_audit_lifecycle_temperature(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from mcp.server.fastmcp import FastMCP

    from kicad_claude import state
    from kicad_claude.adapters import temp_grade
    from kicad_claude.tools.review import release as release
    from kicad_claude.tools.sourcing import distributors as sourcing

    assert temp_grade.parse_range("-40°C ~ 85°C (TA)") == (-40, 85)
    assert temp_grade.parse_range("- 55 C ~ + 125 C") == (-55, 125)
    assert temp_grade.classify(-40, 125) == "automotive"
    assert temp_grade.classify(0, 70) == "commercial"
    nl_path = tmp_path / "t.net"
    comps = [("U1", "MCU", "COMM-1"), ("U2", "LDO", "IND-1"), ("U3", "SENS", "SPEC-1"),
             ("R1", "10k", "RES-1")]
    body = " ".join(f'(comp (ref "{r}") (value "{v}") (footprint "") (libsource (lib "x") '
                    f'(part "x") (description "")) (fields (field (name "MPN") "{m}")))'
                    for r, v, m in comps)
    nl_path.write_text(f'(export (version "E") (components {body}) (nets))', encoding="utf-8")
    data = {("digikey", "COMM-1"): {"lifecycle": "Active", "stock": 5,
                                    "operating_temp": "0°C ~ 70°C (TA)"},
            ("digikey", "IND-1"): {"lifecycle": "Active", "stock": 5,
                                   "operating_temp": "-40°C ~ 85°C"},
            # the datasheet wins over the distributor field
            ("digikey", "SPEC-1"): {"lifecycle": "Active", "stock": 5,
                                    "operating_temp": "0°C ~ 70°C"},
            ("digikey", "RES-1"): {"lifecycle": "Active", "stock": 5}}
    monkeypatch.setattr(sourcing, "search_source",
                        lambda name, mpn, limit=5: [{"mpn": mpn, **data[(name, mpn)]}]
                        if (name, mpn) in data else [])
    specs = tmp_path / "datasheets" / "specs"
    specs.mkdir(parents=True)
    (specs / "SPEC-1.json").write_text(json.dumps({
        "mpn": "SPEC-1", "pdf": {"file": "x.pdf", "sha256": ""},
        "recommended": {"T_A": {"min": -40, "max": 125, "unit": "C", "page": 3}}}),
        encoding="utf-8")
    monkeypatch.setattr(state, "get_active", lambda: SimpleNamespace(path=tmp_path))
    mcp = FastMCP("t")
    release.register(mcp)
    fn = mcp._tool_manager.get_tool("audit_lifecycle").fn
    r = fn(sources=["digikey"], temp_range_c=[-40, 85], netlist_path=str(nl_path))
    got = {(f["rule"], f["ref"]) for f in r["findings"]}
    assert ("LC-TEMP", "U1") in got and ("LC-TEMP", "U2") not in got
    assert ("LC-TEMP", "U3") not in got and ("LC-TEMP-NODATA", "R1") in got
    parts = {p["mpn"]: p for p in r["parts"]}
    assert parts["SPEC-1"]["temperature"]["grade"] == "automotive"
    assert parts["SPEC-1"]["temperature"]["source"].startswith("datasheet SPEC-1 p.3")
    assert parts["COMM-1"]["temperature"]["grade"] == "commercial"
    with pytest.raises(ValueError):
        fn(sources=["digikey"], temp_range_c=[85, -40], netlist_path=str(nl_path))


def test_audit_lifecycle_alternatives(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    from kicad_claude.tools.review import release as release
    from kicad_claude.tools.sourcing import distributors as sourcing

    nl_path = tmp_path / "a.net"
    body = " ".join(f'(comp (ref "{r}") (value "x") (footprint "") (libsource (lib "x") '
                    f'(part "x") (description "")) (fields (field (name "MPN") "{m}")))'
                    for r, m in (("U1", "LM1117IMP-3.3"), ("U2", "ZZ9000-OLD")))
    nl_path.write_text(f'(export (version "E") (components {body}) (nets))', encoding="utf-8")
    ti = "Texas Instruments"
    exact = {
        ("digikey", "LM1117IMP-3.3"): [{"mpn": "LM1117IMP-3.3", "lifecycle": "Obsolete",
                                        "stock": 0, "manufacturer": ti,
                                        "description": "IC REG LINEAR 3.3V SOT-223-4"}],
        ("mouser", "LM1117IMP-3.3"): [{"mpn": "LM1117IMP-3.3", "lifecycle": "Obsolete",
                                       "stock": 0, "manufacturer": ti,
                                       "suggested_replacement": "LM1117IMPX-3.3/NOPB"}],
        ("digikey", "ZZ9000-OLD"): [{"mpn": "ZZ9000-OLD", "lifecycle": "Obsolete", "stock": 0}],
        ("digikey", "LM1117"): [
            {"mpn": "LM1117MPX-3.3/NOPB", "lifecycle": "Active", "stock": 900,
             "manufacturer": ti, "description": "IC REG LINEAR 3.3V SOT-223-4"},
            {"mpn": "LM1117DT-3.3", "lifecycle": "Active", "stock": 50,
             "manufacturer": "STMicroelectronics", "description": "LDO SOT-223"},
            {"mpn": "LM1117MP-5.0", "lifecycle": "Obsolete", "stock": 0,
             "manufacturer": ti, "description": "SOT-223-4"},
            {"mpn": "LM1117DTX-3.3/NOPB", "lifecycle": "Active", "stock": 10,
             "manufacturer": ti, "description": "IC REG LINEAR TO-252-3"}],
    }
    monkeypatch.setattr(sourcing, "search_source",
                        lambda name, q, limit=5: exact.get((name, q), []))
    mcp = FastMCP("t")
    release.register(mcp)
    r = mcp._tool_manager.get_tool("audit_lifecycle").fn(sources=["digikey", "mouser"],
                                                         netlist_path=str(nl_path))
    parts = {p["mpn"]: p for p in r["parts"]}
    alts = parts["LM1117IMP-3.3"]["alternatives"]
    assert [a["mpn"] for a in alts] == ["LM1117IMPX-3.3/NOPB", "LM1117MPX-3.3/NOPB"]
    assert alts[0]["why"] == "suggested replacement"
    assert alts[1]["stock"] == 900 and alts[1]["source"] == "digikey"
    assert parts["ZZ9000-OLD"]["alternatives"] == []
    eol = next(f for f in r["findings"] if f["rule"] == "LC-EOL" and f["ref"] == "U1")
    assert "LM1117MPX-3.3/NOPB" in eol["message"]


# ---- assembly review + cross-verify ------------------------------------------- #


def _asm_board(tmp_path, extra=""):
    def fp(ref, x, y, side="F", attr="smd", crt=(-1, -1, 1, 1), pads=(("1", "A"), ("2", "B")),
           silk=True, lib="L:R", value="X", mpn=""):
        lay = "F" if side == "F" else "B"
        c = f'(fp_rect (start {crt[0]} {crt[1]}) (end {crt[2]} {crt[3]}) (layer "{lay}.CrtYd"))'
        s = f'(fp_line (start -1 -1) (end 1 -1) (layer "{lay}.SilkS"))' if silk else ""
        ps = " ".join(f'(pad "{n}" smd rect (at {i - 0.5} 0) (size 0.4 0.4) (layers "{lay}.Cu") '
                      f'(net "{net}"))' for i, (n, net) in enumerate(pads))
        props = f'(property "Reference" "{ref}") (property "Value" "{value}")' + (
            f' (property "MPN" "{mpn}")' if mpn else "")
        return (f'(footprint "{lib}" (layer "{lay}.Cu") (at {x} {y}) {props} (attr {attr}) '
                f'{c} {s} {ps})')
    items = [fp("R1", 5, 5), fp("R2", 6.5, 5),                     # overlap (0.5 mm)
             fp("R3", 10, 5), fp("R4", 12, 5),                     # touching: fine
             fp("C1", 10, 10, side="B"),                           # SMD on the bottom
             fp("D1", 15, 10, silk=False, lib="L:D"),              # polarized, no silk
             fp("J1", 0.5, 15, attr="through_hole", lib="L:Conn"), # beyond edge, THT
             fp("U1", 15, 15, pads=(("1", "A"), ("2", "B"), ("3", "C")), lib="L:SOT23",
                value="LDO", mpn="BOARD-MPN")]
    text = ('(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal) (2 "B.Cu" signal)) '
            '(gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts")) ' + " ".join(items) + extra + ")")
    p = tmp_path / "a.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    from kicad_claude.adapters import sch_io
    return sch_io.parse_file(p)


def test_assembly_checks(tmp_path):
    from kicad_claude.adapters import assembly_review as ar
    tree = _asm_board(tmp_path)
    r = ar.check_assembly(tree, "top")
    got = {(f["rule"], f["ref"]) for f in r["findings"]}
    assert ("AS-CRTYD-OVERLAP", "R1,R2") in got
    assert not any(f["rule"] == "AS-CRTYD-OVERLAP" and "R3" in f["ref"] for f in r["findings"])
    assert ("AS-SIDE", "C1") in got and ("AS-SILK-POLARITY", "D1") in got
    assert ("AS-EDGE", "J1") in got and ("AS-THT", "J1") in got
    assert any(f["rule"] == "AS-FIDUCIAL" for f in r["findings"])
    r = ar.check_assembly(tree, "both", min_spacing_mm=0.5)
    assert ("AS-CRTYD-SPACING", "R3,R4") in {(f["rule"], f["ref"]) for f in r["findings"]}
    assert not any(f["rule"] == "AS-SIDE" for f in r["findings"])


def test_cross_verify(tmp_path):
    from kicad_claude.adapters import assembly_review as ar
    tree = _asm_board(tmp_path)
    nl = _nl(tmp_path, "x", [("R1", "X", "L:R", ""), ("R9", "1k", "L:R", ""),
                             ("U1", "LDO", "L:SOT23", "SCH-MPN")],
             {"A": [("R1", "1"), ("U1", "1")], "B": [("R1", "2"), ("U1", "2")],
              "C": [("U1", "3")], "D": [("U1", "4")], "E": [("R9", "1")]})
    r = ar.cross_verify(tree, nl)
    got = {(f["rule"], f["ref"]) for f in r["findings"]}
    assert ("XV-MISSING", "R9") in got and ("XV-EXTRA", "R2") in got
    assert ("XV-PADS", "U1") in got                       # pin 4 has no pad
    assert any(f["rule"] == "XV-FIELD" and "MPN" in f["message"] for f in r["findings"])


def test_lifecycle_farnell_rules():
    from kicad_claude.tools.review import release as release
    assert release.EOL_RE.search("farnell: NO_LONGER_MANUFACTURED")
    assert not release.EOL_RE.search("farnell: NO_LONGER_STOCKED")
    assert release.DIST_DROP_RE.search("farnell: NO_LONGER_STOCKED")


@pytest.mark.skipif(__import__("shutil").which("ngspice") is None, reason="ngspice not on PATH")
def test_what_if_filters(tmp_path):
    from mcp.server.fastmcp import FastMCP

    from kicad_claude.tools.review import release as release
    from tests.test_sim_subcircuits import _board

    nl = _board(tmp_path)                                  # writes s.net into tmp_path
    mcp = FastMCP("t")
    release.register(mcp)
    w = mcp._tool_manager.get_tool("what_if").fn(changes={"R4": "10"},
                                                 netlist_path=str(tmp_path / "s.net"))
    assert nl.components["R4"]["value"] == "1"             # the file is not touched
    lad = w["filters"][0]
    assert lad["ladder"] == "+5V -> +5V_F"
    assert lad["peaking_db"][0] != lad["peaking_db"][1]


def test_heights_and_pin1(tmp_path):
    from kicad_claude.adapters import assembly_review as ar
    from kicad_claude.adapters import sch_io
    step = tmp_path / "tall.step"
    step.write_text("#1=CARTESIAN_POINT('',(-1.,-1.,0.));\n#2=CARTESIAN_POINT('',(1.,1.,6.5));\n")
    model = f'(model "{step.as_posix()}" (offset (xyz 0 0 0.5)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))'
    lying = model.replace("(rotate (xyz 0 0 0))", "(rotate (xyz 90 0 0))")

    def fp(ref, x, y, model="", silk="", lib="L:D"):
        return (f'(footprint "{lib}" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}") '
                f'(attr smd) (fp_rect (start -1 -1) (end 1 1) (layer "F.CrtYd")) {silk} '
                '(pad "1" smd rect (at -0.6 0) (size 0.4 0.4) (layers "F.Cu") (net "A")) '
                f'(pad "2" smd rect (at 0.6 0) (size 0.4 0.4) (layers "F.Cu") (net "B")) {model})')
    bar = '(fp_line (start -1.1 -0.8) (end -1.1 0.8) (layer "F.SilkS"))'      # cathode bar
    box = ('(fp_line (start -1 -0.9) (end 1 -0.9) (layer "F.SilkS")) '
           '(fp_line (start -1 0.9) (end 1 0.9) (layer "F.SilkS"))')            # symmetric
    items = [fp("D1", 1.5, 10, model, bar), fp("D2", 10, 10, "", box),
             fp("U3", 15, 15, lying, bar, lib="L:U"),
             fp("D4", 18, 5, '(model "${NOPE}/x.step")', bar)]
    p = tmp_path / "h.kicad_pcb"
    p.write_text('(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal) (2 "B.Cu" signal)) '
                 '(gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts")) ' + " ".join(items) + ")",
                 encoding="utf-8")
    r = ar.check_assembly(sch_io.parse_file(p), "top", max_height_top_mm=6.0)
    h = {x["ref"]: x for x in r["heights"]}
    assert h["D1"]["height_mm"] == pytest.approx(7.0)
    assert h["U3"]["approximate"] and h["U3"]["height_mm"] == pytest.approx(1.5)   # lying flat
    got = {(f["rule"], f["ref"]) for f in r["findings"]}
    assert ("AS-HEIGHT", "D1") in got and ("AS-TALL-EDGE", "D1") in got
    assert ("AS-PIN1-MARK", "D2") in got and ("AS-PIN1-MARK", "D1") not in got
    assert ("AS-3D-NONE", "D2") in got and ("AS-3D-MISSING", "D4") in got


def test_kicad_3dmodel_var(tmp_path, monkeypatch):
    from kicad_claude.utils import kicad_config
    share = tmp_path / "KiCad" / "10.0" / "share" / "kicad"
    (share / "footprints").mkdir(parents=True)
    (share / "3dmodels").mkdir()
    monkeypatch.setattr(kicad_config, "_platform_default_footprint_dirs",
                        lambda: [share / "footprints"])
    monkeypatch.setattr(kicad_config, "_platform_default_symbol_dirs", lambda: [])
    v = kicad_config._install_dir_vars()
    assert v["KICAD10_3DMODEL_DIR"] == str(share / "3dmodels")


def test_what_if_emc(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    from kicad_claude import state
    from kicad_claude.tools.review import release as release
    from tests.test_sim_subcircuits import _board

    _board(tmp_path)
    _asm_board(tmp_path)
    monkeypatch.setattr(state, "get_active_board_path", lambda: tmp_path / "a.kicad_pcb")
    mcp = FastMCP("t")
    release.register(mcp)
    w = mcp._tool_manager.get_tool("what_if").fn(
        changes={"R1": "200k"}, filters=False, emc=True, fsw={"U1": 1e6},
        rail_voltages={"VIN": 12.0}, netlist_path=str(tmp_path / "s.net"))
    assert w["dividers"] and w["emc"]["note"].startswith("board copper unchanged")
    assert isinstance(w["emc"]["findings_new"], list)


def _dfm_board(tmp_path, track_w=0.2, via=(0.6, 0.3), extra=""):
    pads = ('(pad "1" smd rect (at -0.8 0) (size 0.8 0.9) (layers "F.Cu") (net "A")) '
            '(pad "2" smd rect (at 0.8 0) (size 0.8 0.9) (layers "F.Cu") (net "B"))')
    text = ('(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal) (2 "B.Cu" signal)) '
            '(gr_rect (start 0 0) (end 30 30) (layer "Edge.Cuts")) '
            f'(footprint "R:R_0603" (layer "F.Cu") (at 10 10) (property "Reference" "R1") {pads}) '
            f'(segment (start 10.8 10) (end 20 10) (width {track_w}) (layer "F.Cu") (net "B")) '
            f'(via (at 20 20) (size {via[0]}) (drill {via[1]}) (layers "F.Cu" "B.Cu") (net "B")) '
            + extra + ")")
    p = tmp_path / "d.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    from kicad_claude.adapters import sch_io
    return sch_io.parse_file(p)


def test_dfm_against_preset(tmp_path):
    from kicad_claude.adapters import dfm
    from kicad_claude.adapters import project_settings as ps

    rules = ps.FAB_PRESETS["jlcpcb_2l_default"]["rules"]
    ok = dfm.analyze(_dfm_board(tmp_path), "jlcpcb_2l_default", rules)
    assert ok["counts"] == {"error": 0, "warning": 0} and ok["dfm_score"] == 100
    thin = dfm.analyze(_dfm_board(tmp_path, track_w=0.1), "jlcpcb_2l_default", rules)
    assert {f["rule"] for f in thin["findings"]} == {"DFM-TRACK"}
    assert thin["dfm_score"] == 90 and thin["deductions"][0]["rule"] == "DFM-TRACK"
    # small via (drill, diameter, ring), a track 0.1 mm from net A's pad, copper at the edge,
    # a via in an SMD pad, silk across a pad
    extra = ('(segment (start 9.2 10.55) (end 9.2 14) (width 0.2) (layer "F.Cu") (net "C")) '
             '(segment (start 0.1 5) (end 0.1 8) (width 0.2) (layer "F.Cu") (net "D")) '
             '(via (at 9.2 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "A")) '
             '(footprint "X:Y" (layer "F.Cu") (at 25 25) (property "Reference" "D1") '
             '(fp_line (start -2 0) (end 2 0) (layer "F.SilkS")) '
             '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "E")))')
    bad = dfm.analyze(_dfm_board(tmp_path, via=(0.35, 0.15), extra=extra), "jlcpcb_2l_default",
                      rules)
    rules_hit = {f["rule"] for f in bad["findings"]}
    assert {"DFM-DRILL", "DFM-VIA", "DFM-RING", "DFM-SPACING", "DFM-EDGE",
            "DFM-VIA-IN-PAD", "DFM-SILK"} <= rules_hit
    assert bad["dfm_score"] < thin["dfm_score"]
    assert any("solder-mask web" in n for n in bad["not_checked"])


def test_review_assembly_dfm_preset(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    from kicad_claude import state
    from kicad_claude.tools.review import release as release

    _dfm_board(tmp_path, track_w=0.1)
    monkeypatch.setattr(state, "get_active_board_path", lambda: tmp_path / "d.kicad_pcb")
    mcp = FastMCP("t")
    release.register(mcp)
    fn = mcp._tool_manager.get_tool("review_assembly").fn
    r = fn(dfm_preset="jlcpcb_2l_default", heights=False, cross_verify=False)
    assert r["dfm"]["dfm_score"] == 90
    assert any(f["rule"] == "DFM-TRACK" for f in r["findings"])
    assert "dfm" not in fn(heights=False, cross_verify=False)
    with pytest.raises(ValueError):
        fn(dfm_preset="nope", heights=False, cross_verify=False)


def test_assembly_complexity_orders_boards(tmp_path):
    from kicad_claude.adapters import assembly_review as ar
    from kicad_claude.adapters import sch_io

    def board(name, fps):
        p = tmp_path / name
        p.write_text('(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal) (2 "B.Cu" signal)) '
                     + " ".join(fps) + ")", encoding="utf-8")
        return sch_io.parse_file(p)

    def fp(ref, lib, side="F", attr="smd", mpn=None):
        lay = f"{side}.Cu"
        return (f'(footprint "{lib}" (layer "{lay}") (at 5 5) (property "Reference" "{ref}") '
                f'(property "Value" "{mpn or ref}") (attr {attr}) '
                f'(pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "{lay}")))')
    simple = board("s.kicad_pcb", [fp(f"R{i}", "Resistor_SMD:R_1206_3216Metric", mpn="10k")
                                   for i in range(5)])
    hard = board("h.kicad_pcb", [
        fp("U1", "Package_BGA:BGA-256_17x17mm_Layout16x16_P1.0mm"),
        fp("U2", "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.4mm"),
        fp("C1", "Capacitor_SMD:C_0201_0603Metric"),
        fp("C2", "Capacitor_SMD:C_0402_1005Metric", side="B"),
        fp("J1", "Connector:Conn_01x04", attr="through_hole")])
    a, b = ar.complexity(simple), ar.complexity(hard)
    assert a["score"] < b["score"]
    assert a["unique_parts"] == 1 and a["smallest_passive"] == "1206" and a["sides"] == ["F"]
    assert b["smallest_passive"] == "0201" and b["sides"] == ["B", "F"] and b["tht"] == 1
    assert b["fine_pitch"] == [{"ref": "U2", "pitch_mm": 0.4}]
    assert set(b["bga_or_exposed_pad"]) == {"U1", "U2"}
    assert {f["factor"] for f in b["factors"]} >= {"both sides populated", "THT with SMD"}


def test_fab_release_gate_blocks_on_dfm_and_thermal(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mcp.server.fastmcp import FastMCP

    from kicad_claude import state
    from kicad_claude.adapters import kicad_cli
    from kicad_claude.tools.review import release as release
    from tests.test_emc_si_board_thermal import _qfn
    from tests.test_review_ratings_circuits_bom import _buck

    _buck(tmp_path)                                            # s.net
    _dfm_board(tmp_path, track_w=0.1, extra=_qfn(20, 25, 0))    # d.kicad_pcb + QFN U1
    proj = SimpleNamespace(path=tmp_path, name="p", sch_path=tmp_path / "p.kicad_sch")
    monkeypatch.setattr(state, "get_active", lambda: proj)
    monkeypatch.setattr(state, "get_active_board_path", lambda: tmp_path / "d.kicad_pcb")
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: {"errors": 0, "warnings": 0})
    monkeypatch.setattr(kicad_cli, "run_drc", lambda *a, **k: {"errors": 0, "warnings": 0})
    mcp = FastMCP("t")
    release.register(mcp)
    fn = mcp._tool_manager.get_tool("fab_release_gate").fn
    r = fn(preset="jlcpcb_2l_default", loads={"+5V": 3.0}, rail_voltages={"VIN": 12, "+5V": 5},
           netlist_path=str(tmp_path / "s.net"))
    assert r["verdict"] == "NOT FAB-READY"
    assert any(b.startswith("DFM-TRACK") for b in r["blockers"])
    assert r["gates"]["dfm"]["dfm_score"] < 100
    assert any(w.startswith("TH-VIAS U1") for w in r["warnings"])    # thermal ran
    assert "thermal" in r["gates"]
    plain = fn(rail_voltages={"VIN": 12}, netlist_path=str(tmp_path / "s.net"))
    assert "dfm" not in plain["gates"] and "thermal" not in plain["gates"]


def test_dfm_round_pads_are_round(tmp_path):
    """TO-92 pads (1.3 mm circles on a 1.27 mm diagonal pitch) are 0.5 mm apart:
    as squares their corners would touch."""
    from kicad_claude.adapters import dfm
    from kicad_claude.adapters import project_settings as ps

    pads = " ".join(f'(pad "{n}" thru_hole circle (at {x} {y}) (size 1.3 1.3) (drill 0.75) '
                    f'(layers "*.Cu") (net "N{n}"))'
                    for n, x, y in (("1", 0, 0), ("2", 1.27, -1.27), ("3", 2.54, 0)))
    extra = f'(footprint "P:TO-92" (layer "F.Cu") (at 15 20) (property "Reference" "Q1") {pads})'
    r = dfm.analyze(_dfm_board(tmp_path, extra=extra), "jlcpcb_2l_default",
                    ps.FAB_PRESETS["jlcpcb_2l_default"]["rules"])
    assert not [f for f in r["findings"] if f["rule"] == "DFM-SPACING"]
