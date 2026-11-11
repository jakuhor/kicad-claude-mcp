"""Project datasheets: sync, extraction cache, consumers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from kicad_claude.adapters import datasheet_index as dsi
from kicad_claude.adapters import datasheet_specs as dss


def _comp(ref, value, mpn="", datasheet="", lib="Device:X"):
    lib_name, part = lib.split(":")
    fields = f'(field (name "MPN") "{mpn}")' if mpn else ""
    return (f'(comp (ref "{ref}") (value "{value}") (footprint "") (datasheet "{datasheet}")'
            f' (libsource (lib "{lib_name}") (part "{part}") (description ""))'
            f' (fields {fields}))')


def _netlist(tmp_path, comps, nets=""):
    p = tmp_path / "n.net"
    p.write_text(f'(export (version "E") (components {" ".join(comps)}) (nets {nets}))',
                 encoding="utf-8")
    return p


TEXTS = {
    "8471fd.pdf": ["LT8471 dual converter", "p2"],
    "xal4000.pdf": ["XAL4000 series: XAL4020-222ME 2.2 uH"],
    "TVS-SMF-1103.pdf": ["SMF5.0A ... SMF24A ... SMF58A"],
    "HL-SMA-KE-02.pdf": [""],                     # image-only scan
    "spare.pdf": ["unrelated"],
}


@pytest.fixture
def ds_dir(tmp_path, monkeypatch):
    d = tmp_path / "datasheets"
    d.mkdir()
    for name in TEXTS:
        (d / name).write_bytes(b"%PDF-1.4 " + name.encode())
    (d / "ingest").mkdir()
    (d / "ingest" / "old.pdf").write_bytes(b"%PDF-1.4 old")
    monkeypatch.setattr(dsi, "pdf_pages", lambda path, digest=None: TEXTS.get(path.name, [""]))
    return d


def _tools():
    from kicad_claude.tools import datasheets
    mcp = FastMCP("t")
    datasheets.register(mcp)
    return {n: mcp._tool_manager.get_tool(n).fn for n in
            ("sync_datasheets", "get_datasheet_extraction_task",
             "save_datasheet_extraction", "get_datasheet_specs")}


def test_stem_and_score():
    assert dsi.stem("LT8471IUFD#TRPBF") == "LT8471"
    assert dsi.stem("TPS7A4701MRGWREP") == "TPS7A4701"
    assert dsi.stem("150060RS75000") == "150060RS75000"
    assert dsi.content_score("XAL4020-222MEC", dsi.norm("XAL4020-222ME 2.2uH")) == 12
    assert dsi.content_score("XAL4020-222MEC", dsi.norm("XAL4000 family")) == 0
    assert dsi.url_basename("https://x/y/8471fd.pdf?a=1") == "8471fd.pdf"
    assert dsi.url_basename("~") == ""


def test_sync_match_assign_and_manifest(tmp_path, ds_dir):
    nl = _netlist(tmp_path, [
        _comp("U2", "LT8471", "LT8471IUFD#TRPBF", "https://www.analog.com/x/8471fd.pdf"),
        _comp("L3", "2.2u", "XAL4020-222MEC"), _comp("L4", "2.2u", "XAL4020-222MEC"),
        _comp("D6", "SMF24A", "SMF24A"),
        _comp("J3", "SMA", "HL-SMA-KE-02"), _comp("J10", "SMA", "HL-SMA-KE-02"),
        _comp("J1", "USB", "USB4105-GF-A"),
        _comp("J2", "HDR"),                                   # no MPN
        _comp("R1", "10k", "RC0603FR-0710KL"),                # skipped prefix
    ])
    t = _tools()
    r = t["sync_datasheets"](download=False, dest_dir=str(ds_dir), netlist_path=str(nl))
    p = r["parts"]
    assert p["LT8471IUFD#TRPBF"]["how"] == "datasheet_field"
    assert p["XAL4020-222MEC"]["file"] == "xal4000.pdf" and p["XAL4020-222MEC"]["how"] == "content"
    assert p["XAL4020-222MEC"]["refs"] == ["L3", "L4"]
    assert p["SMF24A"]["file"] == "TVS-SMF-1103.pdf"
    assert p["HL-SMA-KE-02"]["how"] == "filename"
    assert p["HL-SMA-KE-02"]["refs"] == ["J3", "J10"]          # natural order
    assert p["USB4105-GF-A"]["status"] == "missing" and r["unresolved"] == ["USB4105-GF-A"]
    assert r["no_mpn"] == ["J2"] and "RC0603FR-0710KL" not in p
    assert r["orphans"] == ["spare.pdf"]                       # ingest/ not read
    man = json.loads((ds_dir / dsi.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert man["parts"]["SMF24A"]["file"] == "TVS-SMF-1103.pdf"

    # second run keeps; assign overrides; a part that left the schematic is flagged
    nl2 = _netlist(tmp_path, [
        _comp("U2", "LT8471", "LT8471IUFD#TRPBF"), _comp("D6", "SMF24A", "SMF24A")])
    r2 = t["sync_datasheets"](download=False, dest_dir=str(ds_dir), netlist_path=str(nl2),
                              assign={"SMF24A": "spare.pdf"})
    assert r2["parts"]["LT8471IUFD#TRPBF"]["status"] == "kept"
    assert r2["parts"]["SMF24A"] == {"status": "assigned", "file": "spare.pdf", "refs": ["D6"]}
    man = json.loads((ds_dir / dsi.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert man["parts"]["XAL4020-222MEC"]["in_schematic"] is False
    with pytest.raises(ValueError):
        t["sync_datasheets"](download=False, dest_dir=str(ds_dir), netlist_path=str(nl2),
                             assign={"SMF24A": "nope.pdf"})


def test_sync_download(tmp_path, ds_dir, monkeypatch):
    from kicad_claude.adapters import datasheet_fetch
    from kicad_claude.tools.sourcing import distributors as sourcing

    monkeypatch.setattr(sourcing, "datasheet_candidates", lambda mpn, s, url="": (
        [{"source": "url", "url": url}] if url else [], {}))
    monkeypatch.setattr(datasheet_fetch, "try_download",
                        lambda url, timeout=30.0: (b"%PDF-1.7 usb", ""))
    nl = _netlist(tmp_path, [_comp("J1", "USB", "USB4105-GF-A", "https://gct.co/usb4105.pdf")])
    r = _tools()["sync_datasheets"](dest_dir=str(ds_dir), netlist_path=str(nl))
    s = r["parts"]["USB4105-GF-A"]
    assert s["status"] == "downloaded" and s["file"] == "usb4105.pdf"
    assert (ds_dir / "usb4105.pdf").read_bytes().startswith(b"%PDF")


PAGES = ["Pin functions", "8.5 Electrical Characteristics VFB 792 800 808 mV fSW 450 500 550 kHz"]


def _good():
    return {
        "category": "regulator_switching",
        "pins": [{"number": "8", "name": "FB"}],
        "regulator": {"vref": {"min": 0.792, "typ": 0.8, "max": 0.808, "unit": "V", "page": 2},
                      "fsw": {"typ": 500000, "unit": "Hz", "page": 2,
                              "quote": "fSW 450 500 550 kHz"},
                      "en_active": "high",
                      "channels": {"1": {"vref": {"typ": 0.8, "unit": "V", "page": 2}}}},
        "i2c": {"addresses": ["0x48"], "page": 1},
    }


def test_validate():
    errors, warnings = dss.validate(_good(), PAGES)
    assert errors == [] and warnings == []

    bad = _good()
    bad["regulator"]["vref"] = {"min": 0.9, "typ": 0.8, "unit": "mV"}
    bad["category"] = "gizmo"
    bad["i2c"]["addresses"] = ["0x90"]
    bad["regulator"]["en_active"] = "maybe"
    errors, _ = dss.validate(bad, PAGES)
    text = " ".join(errors)
    for needle in ("min <= typ", "SI base unit", "page: required", "category",
                   "7-bit", "en_active"):
        assert needle in text, needle

    wrong_page = _good()
    wrong_page["regulator"]["vref"]["page"] = 1
    wrong_page["regulator"]["fsw"]["quote"] = "made up"
    errors, warnings = dss.validate(wrong_page, PAGES)
    assert errors == []
    assert any("regulator.vref" in w and "not found" in w for w in warnings)
    assert any("quote" in w for w in warnings)
    assert dss.validate({**_good(), "regulator": {"vref": {"typ": 0.8, "unit": "V", "page": 9}}},
                        PAGES)[0]


def test_number_scaling():
    assert dss._number_on_page(0.075, "dropout 75 mV")
    assert dss._number_on_page(500000, "500 kHz")
    assert dss._number_on_page(2.2e-6, "2.2 µH")
    assert not dss._number_on_page(0.81, "VFB 800 mV")


def test_select_pages():
    pages = ["title", "app info output voltage output voltage output voltage output voltage "
             "output voltage output voltage", "Absolute Maximum Ratings", "x",
             "Electrical Characteristics (continued)"]
    assert dss.select_pages(pages, limit=3) == [1, 3, 5]


def test_extraction_roundtrip(tmp_path, ds_dir, monkeypatch):
    monkeypatch.setattr(dsi, "pdf_pages", lambda path, digest=None: PAGES)
    nl = _netlist(tmp_path, [_comp("U2", "LT8471", "LT8471IUFD#TRPBF",
                                   "https://www.analog.com/x/8471fd.pdf")],
                  '(net (code "1") (name "FB") (node (ref "U2") (pin "8") '
                  '(pinfunction "FB1_8") (pintype "input")))')
    t = _tools()
    t["sync_datasheets"](download=False, dest_dir=str(ds_dir), netlist_path=str(nl))
    task = t["get_datasheet_extraction_task"]("LT8471IUFD#TRPBF", dest_dir=str(ds_dir),
                                              netlist_path=str(nl))
    assert task["pdf"].endswith("8471fd.pdf") and task["page_count"] == 2
    assert task["schematic"]["pins"]["U2"]["8"] == {"name": "FB1", "net": "FB"}
    assert "SV (spec value)" in task["schema"]

    bad = t["save_datasheet_extraction"]("LT8471IUFD#TRPBF", {"category": "x"},
                                         dest_dir=str(ds_dir))
    assert bad["saved"] is False and bad["errors"]
    ok = t["save_datasheet_extraction"]("LT8471IUFD#TRPBF", _good(), dest_dir=str(ds_dir))
    assert ok["saved"] is True
    with pytest.raises(FileExistsError):
        t["save_datasheet_extraction"]("LT8471IUFD#TRPBF", _good(), dest_dir=str(ds_dir))
    spec = t["get_datasheet_specs"]("LT8471IUFD#TRPBF", dest_dir=str(ds_dir))
    assert spec["found"] and spec["stale"] is False and spec["pdf"]["file"] == "8471fd.pdf"
    assert t["get_datasheet_specs"](dest_dir=str(ds_dir))["counts"]["extracted"] == 1

    (ds_dir / "8471fd.pdf").write_bytes(b"%PDF-1.4 revised")
    assert t["get_datasheet_specs"]("LT8471IUFD#TRPBF", dest_dir=str(ds_dir))["stale"] is True


def test_power_tree_uses_specs(tmp_path, monkeypatch):
    """analyze_power_tree takes Vref from datasheets/specs when not passed."""
    from kicad_claude import state
    from kicad_claude.tools.review import design as review
    from tests.test_review_power_and_connectivity import _netlist as pt_netlist

    proj = tmp_path / "proj"
    ds = proj / "datasheets"
    ds.mkdir(parents=True)
    (ds / "buck.pdf").write_bytes(b"%PDF-1.4 buck")
    monkeypatch.setattr(dsi, "pdf_pages", lambda path, digest=None: PAGES)
    nlp = pt_netlist(tmp_path)
    nlp.write_text(nlp.read_text(encoding="utf-8").replace(
        '(ref "U1") (value "BUCK1") (footprint "")',
        '(ref "U1") (value "BUCK1") (footprint "") (datasheet "")', 1).replace(
        '(part "BUCK1") (description "")) (fields)',
        '(part "BUCK1") (description "")) (fields (field (name "MPN") "BUCK1-MPN"))', 1),
        encoding="utf-8")
    rec = {"category": "regulator_switching",
           "regulator": {"vref": {"typ": 0.8, "unit": "V", "page": 2}}}
    dss.save(ds, "BUCK1-MPN", rec, "buck.pdf", dsi.sha256(ds / "buck.pdf"), "test")
    monkeypatch.setattr(state, "get_active_or_none", lambda: SimpleNamespace(path=proj))

    mcp = FastMCP("t")
    review.register(mcp)
    res = mcp._tool_manager.get_tool("analyze_power_tree").fn(netlist_path=str(nlp))
    u1 = next(r for r in res["regulators"] if r["name"] == "U1")
    assert u1["vout"]["value"] == pytest.approx(4.8)
    assert u1["vout"]["vref_source"] == "datasheet BUCK1-MPN p.2 (buck.pdf)"
    assert not any(f["rule"] == "PT-VOUT-VREF" and f["ref"] == "U1" for f in res["findings"])
    # caller wins
    res = mcp._tool_manager.get_tool("analyze_power_tree").fn(netlist_path=str(nlp),
                                                              vref={"U1": 1.0})
    u1 = next(r for r in res["regulators"] if r["name"] == "U1")
    assert u1["vout"]["vref_source"] == "caller" and u1["vout"]["value"] == pytest.approx(6.0)


def test_datasheets_dir_walks_up(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    board = repo / "hardware" / "base_board"
    board.mkdir(parents=True)
    assert dsi.datasheets_dir(board) == board / "datasheets"      # default
    (repo / "datasheets").mkdir()
    assert dsi.datasheets_dir(board) == repo / "datasheets"       # shared
    (board / "datasheets").mkdir()
    assert dsi.datasheets_dir(board) == board / "datasheets"      # own wins
    (tmp_path / "datasheets").mkdir()                             # above git root
    (board / "datasheets").rmdir()
    assert dsi.datasheets_dir(board) == repo / "datasheets"


def test_log_redacts_api_keys():
    import logging

    from kicad_claude.utils.logging import RedactSecrets, setup_logging

    setup_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    rec = logging.LogRecord("x", logging.INFO, "", 0, 'GET %s "200"',
                            ("https://api.mouser.com/x?apiKey=abc-123&q=1"
                             " https://api.element14.com/c?callInfo.apiKey=s9b&t=2",), None)
    RedactSecrets().filter(rec)
    msg = rec.getMessage()
    assert "abc-123" not in msg and "s9b" not in msg and "q=1" in msg and "apiKey=***" in msg


# ---- datasheet consumers in the power tree --------------------------------- #

def _pt(tmp_path):
    from kicad_claude.adapters import power_review as pr
    from tests.test_review_power_and_connectivity import _netlist as pt_netlist
    return pr, pr.parse_netlist(pt_netlist(tmp_path))


def test_power_tree_datasheet_vref_and_polarity(tmp_path):
    pr, nl = _pt(tmp_path)
    r = pr.analyze(nl, datasheet={"U1": {"vref": (0.8, "ds p.6")}})
    u1 = next(x for x in r["regulators"] if x["name"] == "U1")
    assert u1["vout"] == {"value": pytest.approx(4.8), "vref": 0.8, "vref_source": "ds p.6"}
    r = pr.analyze(nl, datasheet={"U1": {"vref_negative": (-0.8, "ds")}})
    rules = {(f["rule"], f["ref"]) for f in r["findings"]}
    assert ("PT-VREF-POL", "U1") in rules and ("PT-VOUT-VREF", "U1") not in rules
    assert pr.is_negative_rail("/400/-20V") and not pr.is_negative_rail("+5V")


def test_power_tree_limits(tmp_path):
    pr, nl = _pt(tmp_path)
    ds = {"U1": {"vref": (0.8, "ds")},
          "U2": {"limits": {"dropout": (None, 1.2, 2.0, "ds p.6"),
                            "abs_vin": (None, None, 4.0, "ds p.4"),
                            "abs_en": (None, None, 3.6, "ds p.4"),
                            "en_on": (1.0, None, 1.2, "ds p.6")}}}
    r = pr.analyze(nl, datasheet=ds, rail_voltages={"+3V3": 3.3})
    f = {(x["rule"], x["severity"]) for x in r["findings"] if x["ref"] == "U2"}
    assert ("PT-DS-VIN-ABS", "error") in f              # 4.8 V > 4.0 V
    assert ("PT-DS-HEADROOM", "warning") in f           # 1.5 V < 2.0 V
    assert ("PT-DS-EN-ABS", "error") in f               # PG_5V released via R5 to +5V
    assert ("PT-DS-EN", "info") in f
    rv = r["rail_voltages"]
    assert rv["+5V"]["volts"] == pytest.approx(4.8) and rv["+3V3"]["source"] == "caller"
    assert rv["+5V_F"]["source"].endswith("via DNP L2")  # as-designed through DNP


def test_net_voltage_divider(tmp_path):
    pr, nl = _pt(tmp_path)
    v = pr._net_voltage(nl, "Net-(U1-EN)", {"VIN": (12.0, "x")})   # R3 100k / R4 47k
    assert v == pytest.approx(12 * 47 / 147)
    assert pr._net_voltage(nl, "Net-(U1-EN)", {}) is None


def test_power_tree_set_current_and_anyout(tmp_path):
    from kicad_claude.adapters import power_review as pr
    from tests.test_review_power_and_connectivity import _comp as c, _net as n

    comps = [c("U9", "LDO_SET", "Regulator_Linear:LDO_SET"), c("U10", "ANYOUT", "Regulator_Linear:AO"),
             c("R9", "49.9k"), c("C1", "10u", "Device:C"), c("J1", "IN", "Connector:J")]
    nets = [
        n(1, "VIN", ("J1", "1", "", "passive"), ("U9", "1", "IN", "power_in"),
          ("U10", "1", "IN", "power_in")),
        n(2, "GND", ("J1", "2", "", "passive"), ("R9", "2", "", "passive"),
          ("C1", "2", "", "passive"), ("U10", "12", "0P1V", "input"),
          ("U10", "4", "6P4V2", "input")),
        n(3, "+5VA", ("U9", "2", "OUT", "power_out"), ("C1", "1", "", "passive")),
        n(4, "SETN", ("U9", "3", "SET", "passive"), ("R9", "1", "", "passive")),
        n(5, "+7V9", ("U10", "2", "OUT", "power_out"), ("U10", "3", "SENSE", "input")),
        n(6, "unconnected-(U10-0P2V-Pad11)", ("U10", "11", "0P2V", "input")),
    ]
    p = tmp_path / "s.net"
    p.write_text(f'(export (version "E") (components {" ".join(comps)}) (nets {" ".join(nets)}))',
                 encoding="utf-8")
    nl = pr.parse_netlist(p)
    ds = {"U9": {"set_current": (100e-6, "ds p.3")},
          "U10": {"anyout": {"base": (1.4, "ds p.6"),
                             "pins": {"12": (0.1, "p3"), "11": (0.2, "p3"), "4": (6.4, "p3")}}}}
    r = pr.analyze(nl, datasheet=ds)
    regs = {x["name"]: x for x in r["regulators"]}
    assert regs["U9"]["vout"]["value"] == pytest.approx(4.99)
    assert regs["U9"]["vout"]["method"] == "set_current"
    assert not any(f["rule"] == "PT-SET" for f in r["findings"])
    ao = regs["U10"]["vout"]
    assert ao["value"] == pytest.approx(7.9) and ao["method"] == "anyout"
    assert ao["pins_grounded"] == ["0P1V(12)", "6P4V2(4)"]


def test_validate_anyout():
    rec = {"category": "regulator_linear",
           "regulator": {"anyout": {"base": {"typ": 0.8, "unit": "V", "page": 2},
                                    "pins": {"12": {"typ": 0.1, "unit": "V"}}}}}
    errors, _ = dss.validate(rec, PAGES)
    assert any("anyout.pins.12.page" in e for e in errors)
    errors, _ = dss.validate({"category": "regulator_linear",
                              "regulator": {"anyout": {"base": 1}}}, PAGES)
    assert any("anyout" in e for e in errors)


# ---- datasheet-backed connectivity checks ---------------------------------- #

def _cr_nl(tmp_path):
    from kicad_claude.adapters import power_review as pr
    from tests.test_review_power_and_connectivity import _comp as c, _net as n

    comps = [c("U1", "PD", "Interface:PD"), c("U2", "SENSOR", "Sensor:S"),
             c("R1", "2k2"), c("R2", "2k2"), c("R3", "100k"), c("R4", "10k"),
             c("J1", "IN", "Connector:J")]
    nets = [
        n(1, "+3V3", ("J1", "1", "", "passive"), ("R1", "1", "", "passive"),
          ("R2", "1", "", "passive"), ("U1", "1", "VDD", "power_in"),
          ("U2", "1", "VDD", "power_in"), ("R3", "1", "", "passive")),
        n(2, "GND", ("J1", "2", "", "passive"), ("U1", "2", "GND", "power_in"),
          ("U1", "9", "GND", "passive"), ("U2", "2", "VSS", "power_in"),
          ("R4", "2", "", "passive")),
        n(3, "SDA", ("U1", "3", "SDA", "bidirectional"), ("U2", "3", "SDA", "bidirectional"),
          ("R1", "2", "", "passive")),
        n(4, "SCL", ("U1", "4", "SCL", "input"), ("U2", "4", "SCL", "input"),
          ("R2", "2", "", "passive")),
        n(5, "unconnected-(U1-NC-Pad5)", ("U1", "5", "NC", "no_connect")),
        n(6, "DPLUS", ("U1", "6", "NC", "passive"), ("U2", "6", "IO", "bidirectional")),
        n(7, "ENDIV", ("U1", "7", "EN", "input"), ("R3", "2", "", "passive"),
          ("R4", "1", "", "passive")),
        n(8, "unconnected-(U1-X-Pad8)", ("U1", "8", "X", "input")),
    ]
    p = tmp_path / "cr.net"
    p.write_text(f'(export (version "E") (components {" ".join(comps)}) (nets {" ".join(nets)}))',
                 encoding="utf-8")
    return pr.parse_netlist(p)


def _spec(mpn, pins=(), absmax=None, i2c=None):
    return {"mpn": mpn, "pdf": {"file": f"{mpn}.pdf"},
            "pins": [{"number": n, "name": nm, "description": d} for n, nm, d in pins],
            "absolute_max": absmax or {}, **({"i2c": i2c} if i2c else {})}


def test_datasheet_pins_and_abs_max(tmp_path):
    from kicad_claude.adapters import connectivity_review as cr

    nl = _cr_nl(tmp_path)
    u1 = _spec("PD1", pins=[("1", "VDD", ""), ("2", "VSS", ""), ("3", "SDA", ""), ("4", "SCL", ""),
                            ("5", "D-", "Leave this pin unconnected"),
                            ("6", "D+", "Leave this pin unconnected"),
                            ("7", "EN", ""), ("8", "X", "")],
                absmax={"VDD": {"max": 3.6, "unit": "V", "page": 5},
                        "EN": {"min": -0.3, "max": 0.2, "unit": "V", "page": 5}},
                i2c={"addresses": ["0x08"], "page": 4})
    u2 = _spec("SENS", i2c={"addresses": ["0x08"], "page": 2})
    r = cr.analyze(nl, specs={"U1": u1, "U2": u2},
                   volts={"+3V3": (3.3, "caller")})
    f = {(x["rule"], x["ref"]): x["message"] for x in r["findings"]}
    dp = r["datasheet_pins"][0]
    assert dp["missing"] == [] and dp["extra"] == []          # pin 9 on GND = exposed pad
    assert dp["renamed"] == []                                # GND~VSS, NC~leave-unconnected
    assert ("CR-PIN-NC", "U1") in f and "DPLUS" in f[("CR-PIN-NC", "U1")]
    assert ("CR-PIN-ABS", "U1") in f and "EN(7)" in f[("CR-PIN-ABS", "U1")]   # 0.3 V > 0.2 V
    rows = {(x["pin"], x["limit"]): x for x in r["pin_abs_max"]}
    assert rows[("VDD(1)", "VDD")]["ok"] and rows[("EN(7)", "EN")]["volts"] == pytest.approx(0.3)
    dev = {d["ref"]: d for d in r["i2c_buses"][0]["devices"]}
    assert dev["U1"]["address"] == "0x08" and dev["U1"]["address_source"] == "datasheet p.4"
    assert ("CR-I2C-ADDR", "U1,U2") in f                     # different parts, same address

    u1["pins"] = u1["pins"][:7] + [{"number": "10", "name": "VBUS", "description": ""}]
    dp = cr.analyze(nl, ("datasheet_pins",), specs={"U1": u1})["datasheet_pins"][0]
    assert dp["missing"] == ["10"] and dp["extra"] == ["8", "9"]   # 9 < 10: not a pad


def test_names_match():
    from kicad_claude.adapters import connectivity_review as cr
    assert cr._names_match("~{HPI_INT}", "HPI_INT")
    assert cr._names_match("0.4V", "0P4V")
    assert cr._names_match("6.4V", "6P4V1")
    assert cr._names_match("SS", "SS/PG")
    assert cr._names_match("GND", "VSS")
    assert cr._names_match("NC", "DNU1")
    assert cr._names_match("NC", "n.c.")
    assert not cr._names_match("FB", "EN")
    assert cr._abs_keys_for("IN", ["VIN", "EN"]) == ["VIN"]
    assert cr._abs_keys_for("CC1", ["CC", "VIN"]) == ["CC"]
    assert cr._abs_keys_for("EN/UV", ["EN"]) == ["EN"]


def test_number_digit_spaced():
    assert dss._number_on_page(30, "VBUS_IN and VDC_OUT pins –– 3 0 V")
    assert dss._number_on_page(26.7, "SMF24A BZ IZ 26.70 29.50")


def test_pin_pair_limits(tmp_path):
    """`between` limits: dual FET with channel-less symbol names, TVS standoff."""
    from kicad_claude.adapters import connectivity_review as cr
    from kicad_claude.adapters import power_review as pr
    from tests.test_review_power_and_connectivity import _comp as c, _net as n

    comps = [c("Q1", "PFET2", "Transistor_FET:Q"), c("D1", "TVS", "Device:D_TVS"),
             c("J1", "IN", "Connector:J")]
    nets = [
        n(1, "VBUS", ("J1", "1", "", "passive"), ("Q1", "5", "D", "passive"),
          ("Q1", "6", "D", "passive"), ("D1", "1", "K", "passive")),
        n(2, "GND", ("J1", "2", "", "passive"), ("D1", "2", "A", "passive")),
        n(3, "MID", ("Q1", "1", "S", "passive"), ("Q1", "3", "S", "passive")),
        n(4, "GATE", ("Q1", "2", "G", "input"), ("Q1", "4", "G", "input")),
        n(5, "OUT", ("Q1", "7", "D", "passive"), ("Q1", "8", "D", "passive")),
    ]
    p = tmp_path / "q.net"
    p.write_text(f'(export (version "E") (components {" ".join(comps)}) (nets {" ".join(nets)}))',
                 encoding="utf-8")
    nl = pr.parse_netlist(p)
    fet = _spec("FET", pins=[("1", "S1", ""), ("2", "G1", ""), ("3", "S2", ""), ("4", "G2", ""),
                             ("5", "D2", ""), ("6", "D2", ""), ("7", "D1", ""), ("8", "D1", "")],
                absmax={"VDS": {"min": -30, "unit": "V", "page": 2, "between": ["D", "S"]},
                        "VGS": {"min": -20, "max": 20, "unit": "V", "page": 2,
                                "between": ["G", "S"]}})
    tvs = _spec("TVS", pins=[("1", "K", ""), ("2", "A", "")])
    tvs["recommended"] = {"VRWM": {"max": 18, "unit": "V", "page": 2, "between": ["K", "A"]}}
    volts = {"VBUS": (20.0, "c"), "MID": (20.0, "c"), "GATE": (-5.0, "c"), "OUT": (-15.0, "c")}
    r = cr.analyze(nl, ("pin_abs_max",), specs={"Q1": fet, "D1": tvs}, volts=volts)
    rows = {(x["ref"], x["pin"]): x for x in r["pin_abs_max"]}
    assert rows[("Q1", "D1(7)-S1(1)")]["volts"] == pytest.approx(-35.0)
    assert not rows[("Q1", "D1(7)-S1(1)")]["ok"]
    assert rows[("Q1", "D2(5)-S2(3)")]["ok"]
    assert not rows[("Q1", "G1(2)-S1(1)")]["ok"]                      # -25 V < -20 V
    f = {(x["rule"], x["ref"]) for x in r["findings"]}
    assert ("CR-PIN-ABS", "Q1") in f and ("CR-PIN-REC", "D1") in f   # 20 V > 18 V standoff
    assert dss.validate({"category": "tvs", "recommended": {
        "VRWM": {"max": 24, "unit": "V", "page": 2, "between": ["K"]}}}, PAGES)[0]


def test_cap_voltage(tmp_path):
    from kicad_claude.adapters import connectivity_review as cr
    from kicad_claude.adapters import power_review as pr
    from tests.test_review_power_and_connectivity import _net as n

    def cap(ref, value, volt=None, dnp=False):
        f = f'(field (name "Voltage") "{volt}")' if volt else ""
        d = ' (property (name "dnp"))' if dnp else ""
        return (f'(comp (ref "{ref}") (value "{value}") (footprint "")'
                f' (libsource (lib "Device") (part "C") (description "")) (fields {f}){d})')

    comps = [cap("C1", "10u", "25V"), cap("C2", "100n", "16V"), cap("C3", "1u/50V"),
             cap("C4", "1u", "6V"), cap("C5", "1u"), cap("C6", "1u", "10V", dnp=True),
             cap("C7", "1u", "50V")]
    nets = [
        n(1, "+20V", ("C1", "1", "", "passive"), ("C2", "1", "", "passive"),
          ("C3", "1", "", "passive"), ("C5", "1", "", "passive"), ("C6", "1", "", "passive")),
        n(2, "GND", ("C1", "2", "", "passive"), ("C2", "2", "", "passive"),
          ("C3", "2", "", "passive"), ("C4", "2", "", "passive"), ("C5", "2", "", "passive"),
          ("C6", "2", "", "passive")),
        n(3, "+5V", ("C4", "1", "", "passive"), ("C7", "1", "", "passive")),
        n(4, "X", ("C7", "2", "", "passive")),
    ]
    p = tmp_path / "c.net"
    p.write_text(f'(export (version "E") (components {" ".join(comps)}) (nets {" ".join(nets)}))',
                 encoding="utf-8")
    nl = pr.parse_netlist(p)
    r = cr.analyze(nl, ("cap_voltage",), volts={"+20V": (20.0, "c"), "+5V": (5.0, "c")})
    cv = r["cap_voltage"]
    rows = {x["ref"]: x for x in cv["checked"]}
    assert rows["C3"]["rating"] == 50 and rows["C4"]["rating"] == 6
    assert cr._rating({"fields": {"Voltage": "6V3"}, "value": ""}, "Voltage") == pytest.approx(6.3)
    assert cr._rating({"fields": {"Voltage": "6.3 V"}, "value": ""}, "Voltage") == pytest.approx(6.3)
    assert cv["no_rating"] == ["C5"] and cv["voltage_unknown"] == ["C7"] and "C6" not in rows
    f = {(x["rule"], x["ref"]) for x in r["findings"]}
    assert ("CR-CAP-V", "C2") in f                    # 20 V on 16 V
    assert ("CR-CAP-DERATE", "C4") in f               # 5 V on 6 V = 83 % > 80 %
    assert ("CR-CAP-DERATE", "C1") not in f           # 80 % is not above 0.8
    r = cr.analyze(nl, ("cap_voltage",), volts={"+20V": (20.0, "c"), "+5V": (5.0, "c")},
                   cap_derating=0.75)
    assert ("CR-CAP-DERATE", "C1") in {(x["rule"], x["ref"]) for x in r["findings"]}


def test_sourcing_import_quiets_http():
    import importlib
    import logging

    logging.getLogger("httpx").setLevel(logging.INFO)
    from kicad_claude.adapters import mouser
    importlib.reload(mouser)
    from kicad_claude.utils.logging import RedactSecrets
    lg = logging.getLogger("httpx")
    assert lg.level == logging.WARNING and any(isinstance(f, RedactSecrets) for f in lg.filters)


@pytest.mark.parametrize("bad", ["../secret.pdf", "ingest/old.pdf", "..", "/etc/x.pdf",
                                 "C:/Windows/x.pdf", ""])
def test_manifest_entry_cannot_point_outside_datasheets(tmp_path, ds_dir, bad):
    """A manifest from a cloned repo must not make the tools read other files (OP-16)."""
    (tmp_path / "secret.pdf").write_bytes(b"%PDF-1.4 secret")
    dsi.save_manifest(ds_dir, {"version": 1, "parts": {"X1": {"file": bad}}})
    t = _tools()
    with pytest.raises(ValueError, match="points outside"):
        t["get_datasheet_extraction_task"]("X1", dest_dir=str(ds_dir))
