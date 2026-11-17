"""Sourcing.

Strategy:
- Unit tests for vendor_import (build synthetic ZIPs at runtime, no network).
- Tool layer: monkeypatch the distributor adapters so tests run offline.
- @pytest.mark.network: real DigiKey / Mouser / TME / Farnell calls.
  Skipped if the matching credentials are absent.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
from dotenv import load_dotenv

from kicad_claude import state
from kicad_claude.adapters import vendor_import
from kicad_claude.adapters.snapeda import manual_fallback_message, part_page_url
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools.library import index as lib_tools
from kicad_claude.tools.sourcing import distributors as sourcing_tools


# Make .env credentials available to network tests.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# ===== vendor_import (unit) ================================================ #


def _make_vendor_zip(target: Path, name: str = "ACME_PART") -> Path:
    """Build a fake SnapEDA-style ZIP with one symbol + one footprint."""
    zip_path = target / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr(
            f"KiCad/{name}.kicad_sym",
            f"""(kicad_symbol_lib
\t(version 20251024)
\t(generator "snapeda")
\t(symbol "{name}"
\t\t(property "Reference" "U" (at 0 0 0))
\t\t(property "Value" "{name}" (at 0 0 0))
\t\t(property "Description" "Synthetic test part" (at 0 0 0))
\t\t(symbol "{name}_1_1"
\t\t\t(pin power_in line (at 0 0 0) (length 1) (name "VCC" (effects (font (size 1 1)))) (number "1" (effects (font (size 1 1)))))
\t\t)
\t)
)
""",
        )
        z.writestr(
            f"KiCad/{name}.pretty/{name}_PKG.kicad_mod",
            f"""(footprint "{name}_PKG"
\t(version 20260206)
\t(generator "snapeda")
\t(layer "F.Cu")
\t(descr "Synthetic test footprint")
\t(tags "test")
)
""",
        )
        z.writestr(f"3D/{name}.step", "ISO-10303-21;\n/* fake */\nEND-ISO-10303-21;\n")
    return zip_path


def test_import_zip_creates_lib_files_and_tables(tmp_path: Path):
    files = write_blank_project(tmp_path / "demo", "demo")
    drop_dir = tmp_path / "drops"
    drop_dir.mkdir()
    zip_path = _make_vendor_zip(drop_dir)

    result = vendor_import.import_zip(zip_path, files["pro"].parent, target_lib="myvendor")

    assert result["symbols_added"] == 1
    assert result["footprints_added"] == 1
    sym_lib = files["pro"].parent / "lib" / "myvendor.kicad_sym"
    pretty = files["pro"].parent / "lib" / "myvendor.pretty"
    assert sym_lib.is_file()
    assert pretty.is_dir()
    assert (pretty / "ACME_PART_PKG.kicad_mod").is_file()

    sym_table = (files["pro"].parent / "sym-lib-table").read_text()
    fp_table = (files["pro"].parent / "fp-lib-table").read_text()
    assert "myvendor" in sym_table
    assert '${KIPRJMOD}/lib/myvendor.kicad_sym' in sym_table
    assert "myvendor" in fp_table
    assert '${KIPRJMOD}/lib/myvendor.pretty' in fp_table


def test_import_zip_idempotent_lib_table(tmp_path: Path):
    files = write_blank_project(tmp_path / "demo", "demo")
    drop_dir = tmp_path / "drops"
    drop_dir.mkdir()
    zip1 = _make_vendor_zip(drop_dir, name="P_ONE")
    zip2 = _make_vendor_zip(drop_dir, name="P_TWO")

    vendor_import.import_zip(zip1, files["pro"].parent, target_lib="vendor")
    vendor_import.import_zip(zip2, files["pro"].parent, target_lib="vendor")

    # Both symbols should be in one lib file
    sym_lib_text = (files["pro"].parent / "lib" / "vendor.kicad_sym").read_text()
    assert "P_ONE" in sym_lib_text
    assert "P_TWO" in sym_lib_text
    # Lib table still has exactly one entry for "vendor"
    sym_table_text = (files["pro"].parent / "sym-lib-table").read_text()
    assert sym_table_text.count('(name "vendor")') == 1


def test_import_zip_rejects_unsafe_target_lib(tmp_path: Path):
    files = write_blank_project(tmp_path / "demo", "demo")
    drop_dir = tmp_path / "drops"
    drop_dir.mkdir()
    zip_path = _make_vendor_zip(drop_dir)
    with pytest.raises(ValueError, match="alphanumeric"):
        vendor_import.import_zip(zip_path, files["pro"].parent, target_lib="my vendor!")


def test_import_zip_no_kicad_assets_raises(tmp_path: Path):
    files = write_blank_project(tmp_path / "demo", "demo")
    bad_zip = tmp_path / "no-kicad.zip"
    with zipfile.ZipFile(bad_zip, "w") as z:
        z.writestr("readme.txt", "no KiCad here")
    with pytest.raises(ValueError, match="no KiCad assets"):
        vendor_import.import_zip(bad_zip, files["pro"].parent)


# ===== SnapEDA helpers ===================================================== #


def test_part_page_url_with_and_without_manufacturer():
    assert "LM358" in part_page_url("LM358")
    assert part_page_url("LM358N", "Texas Instruments").startswith(
        "https://www.snapeda.com/parts/LM358N/"
    )


def test_manual_fallback_message_mentions_url_and_dir():
    msg = manual_fallback_message("LM358N", "Texas Instruments", "/tmp/drop")
    assert "snapeda.com" in msg
    assert "/tmp/drop" in msg
    assert "import_vendor_zip" in msg


# ===== Tool layer ========================================================== #


#: adapter attribute each source is queried through, per `search_source`.
_SEARCH_FN = {
    "digikey": "search_keyword",
    "mouser": "search_part",
    "tme": "search_part",
    "farnell": "search_part",
}


def _stub_source(monkeypatch, name, results=None, err=None):
    """Replace one adapter's search function with a stub or a raiser."""
    mod = getattr(sourcing_tools, name)
    fn_name = _SEARCH_FN[name]
    if err is not None:
        exc = sourcing_tools.source_error(name)

        def boom(*a, **k):
            raise exc(err)

        monkeypatch.setattr(mod, fn_name, boom)
    elif results is not None:
        monkeypatch.setattr(mod, fn_name, lambda *a, **k: list(results))


def _make_mcp(monkeypatch, idx, dk_results=None, mo_results=None,
              dk_err=None, mo_err=None, tme_results=None, tme_err=None,
              fn_results=None, fn_err=None):
    """Build a sourcing-tool mcp with a synthetic index and stubbed APIs."""
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)

    _stub_source(monkeypatch, "digikey", dk_results, dk_err)
    _stub_source(monkeypatch, "mouser", mo_results, mo_err)
    _stub_source(monkeypatch, "tme", tme_results, tme_err)
    _stub_source(monkeypatch, "farnell", fn_results, fn_err)

    mcp = FastMCP("test")
    sourcing_tools.register(mcp)
    return mcp


def _call(mcp, name, **kwargs):
    return mcp._tool_manager.get_tool(name).fn(**kwargs)


def test_check_availability_combines_both_sources(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    dk_hit = {"source": "digikey", "mpn": "LM358N", "stock": 100, "unit_price": 0.50}
    mo_hit = {"source": "mouser", "mpn": "LM358N", "stock": 200, "unit_price": 0.45}
    mcp = _make_mcp(monkeypatch, idx, dk_results=[dk_hit], mo_results=[mo_hit])
    res = _call(mcp, "check_availability", mpn="LM358N", sources=["digikey", "mouser"])
    assert res["digikey"]["stock"] == 100
    assert res["mouser"]["stock"] == 200
    assert res["errors"] == {}
    # Sources that were not asked for stay present but empty.
    assert res["tme"] is None and res["farnell"] is None


def test_check_availability_partial_failure_surfaces_errors(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    dk_hit = {"source": "digikey", "mpn": "LM358N", "stock": 100}
    mcp = _make_mcp(
        monkeypatch, idx,
        dk_results=[dk_hit],
        mo_err="Invalid API Key",
    )
    res = _call(mcp, "check_availability", mpn="LM358N", sources=["digikey", "mouser"])
    assert res["digikey"]["stock"] == 100
    assert res["mouser"] is None
    assert "mouser" in res["errors"]


def test_check_availability_defaults_to_mouser_only(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mo_hit = {"source": "mouser", "mpn": "LM358N", "stock": 200}
    dk_hit = {"source": "digikey", "mpn": "LM358N", "stock": 100}
    mcp = _make_mcp(monkeypatch, idx, dk_results=[dk_hit], mo_results=[mo_hit])
    res = _call(mcp, "check_availability", mpn="LM358N")
    assert res["sources"] == ["mouser"]
    assert res["mouser"]["stock"] == 200
    assert res["digikey"] is None


def test_check_availability_tme_and_farnell(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    tme_hit = {
        "source": "tme", "mpn": "LM358N", "stock": 42,
        "unit_price": 7.5, "currency": "CZK",
    }
    fn_hit = {
        "source": "farnell", "mpn": "LM358N", "stock": 17,
        "unit_price": 9.1, "currency": "CZK",
    }
    mcp = _make_mcp(monkeypatch, idx, tme_results=[tme_hit], fn_results=[fn_hit])
    res = _call(mcp, "check_availability", mpn="LM358N", sources=["tme", "farnell"])
    assert res["sources"] == ["tme", "farnell"]
    assert res["tme"]["stock"] == 42
    assert res["farnell"]["stock"] == 17
    assert res["errors"] == {}


def test_check_availability_tme_failure_surfaces_error(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(monkeypatch, idx, tme_err="bad signature")
    res = _call(mcp, "check_availability", mpn="LM358N", sources=["tme"])
    assert res["tme"] is None
    assert "bad signature" in res["errors"]["tme"]


def test_check_availability_rejects_unknown_source(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(monkeypatch, idx)
    with pytest.raises(ValueError, match="Unknown source"):
        _call(mcp, "check_availability", mpn="LM358N", sources=["rs-online"])


@pytest.mark.parametrize("raw, expected", [
    ("0,420 €", 0.42),
    ("$0.45", 0.45),
    ("12,50 Kč", 12.5),
    ("1 234,50 Kč", 1234.5),
    ("1\xa0234,50\xa0Kč", 1234.5),
    ("$1,234.56", 1234.56),
    ("1.234,56 €", 1234.56),
    ("", None),
    ("N/A", None),
])
def test_mouser_parse_price_handles_locales(raw, expected):
    from kicad_claude.adapters import mouser
    assert mouser._parse_price(raw) == expected


def test_mouser_summarize_part_keeps_czk_price():
    from kicad_claude.adapters import mouser
    part = mouser._summarize_part({
        "ManufacturerPartNumber": "LM358N",
        "AvailabilityInStock": "1 200",
        "PriceBreaks": [{"Quantity": 1, "Price": "11,90 Kč", "Currency": "CZK"}],
    })
    assert part["unit_price"] == 11.9
    assert part["currency"] == "CZK"


def test_parse_sources_orders_and_deduplicates():
    assert sourcing_tools._parse_sources(["farnell", " TME ", "mouser"]) == [
        "mouser", "tme", "farnell"
    ]
    with pytest.raises(ValueError, match="No sources"):
        sourcing_tools._parse_sources(["  "])


def test_enrich_bom_adds_a_column_block_per_source(tmp_path, monkeypatch):
    # The tool resolves the active project even when both paths are explicit.
    write_blank_project(tmp_path / "demo", "demo")
    state.clear_active()
    state.set_active(tmp_path / "demo", "demo")

    bom = tmp_path / "bom.csv"
    bom.write_text("Reference,Value\nU1,LM358N\n", encoding="utf-8")
    out = tmp_path / "enriched.csv"

    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(
        monkeypatch, idx,
        tme_results=[{
            "source": "tme", "mpn": "LM358N", "manufacturer": "TI",
            "stock": 42, "unit_price": 7.5, "currency": "CZK",
            "product_url": "https://www.tme.eu/x",
        }],
        fn_results=[{
            "source": "farnell", "mpn": "LM358N", "manufacturer": "TI",
            "stock": 17, "unit_price": 9.1, "currency": "CZK",
            "product_url": "https://cz.farnell.com/1234",
        }],
    )
    res = _call(
        mcp, "enrich_bom_with_sourcing",
        bom_path=str(bom), output_path=str(out), sources=["tme", "farnell"],
    )
    assert res["sources"] == ["tme", "farnell"]
    assert res["tme_hits"] == 1 and res["farnell_hits"] == 1
    assert res["digikey_hits"] is None and res["mouser_hits"] is None

    import csv as _csv
    with out.open(encoding="utf-8", newline="") as f:
        rows = list(_csv.DictReader(f))
    assert rows[0]["tme_stock"] == "42"
    assert rows[0]["fn_price"] == "9.1"
    assert rows[0]["fn_currency"] == "CZK"
    state.clear_active()


def test_mouser_summary_keeps_every_price_break_and_its_part_number():
    from kicad_claude.adapters import mouser
    part = mouser._summarize_part({
        "ManufacturerPartNumber": "1N4148W-7-F",
        "MouserPartNumber": "621-1N4148W-F",
        "PriceBreaks": [
            {"Quantity": 100, "Price": "0,90 Kč", "Currency": "CZK"},
            {"Quantity": 1, "Price": "3,96 Kč", "Currency": "CZK"},
            {"Quantity": 10, "Price": "2,10 Kč", "Currency": "CZK"},
        ],
    })
    assert part["sku"] == "621-1N4148W-F"
    assert part["price_breaks"] == [{"qty": 1, "unit_price": 3.96},
                                    {"qty": 10, "unit_price": 2.1},
                                    {"qty": 100, "unit_price": 0.9}]


def test_other_sources_return_their_order_code_and_breaks():
    from kicad_claude.adapters import digikey, farnell
    dk = digikey._summarize_product({
        "ManufacturerProductNumber": "LM358N",
        "ProductVariations": [{"DigiKeyProductNumber": "296-1395-5-ND",
                               "StandardPricing": [{"BreakQuantity": 10, "UnitPrice": 0.4},
                                                   {"BreakQuantity": 1, "UnitPrice": 0.5}]}],
    })
    assert dk["sku"] == "296-1395-5-ND"
    assert [b["qty"] for b in dk["price_breaks"]] == [1, 10]
    fn = farnell._summarize_part({"sku": "1234", "prices": [
        {"from": 1, "to": 9, "cost": 0.5}, {"from": 10, "to": 99, "cost": 0.3}]})
    assert fn["sku"] == "1234" and fn["price_breaks"][1] == {"qty": 10, "unit_price": 0.3}
    assert digikey._summarize_product({})["sku"] == ""


def test_price_at_picks_the_break_for_the_quantity():
    hit = {"unit_price": 3.96, "price_breaks": [{"qty": 1, "unit_price": 3.96},
                                                {"qty": 10, "unit_price": 2.1},
                                                {"qty": 100, "unit_price": 0.9}]}
    assert [sourcing_tools.price_at(hit, q) for q in (1, 9, 10, 99, 5000)] == [
        3.96, 3.96, 2.1, 2.1, 0.9]
    moq = {"price_breaks": [{"qty": 10, "unit_price": 2.0}]}
    assert sourcing_tools.price_at(moq, 3) == 2.0  # below a minimum order
    assert sourcing_tools.price_at({"unit_price": 7.5}, 50) == 7.5


def _mouser_post(monkeypatch, resp):
    from kicad_claude.adapters import mouser
    from collections import deque
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    monkeypatch.setattr(mouser, "_calls", deque())
    monkeypatch.setattr(mouser.httpx, "post", lambda *a, **k: resp)
    return mouser


def test_mouser_rate_limit_is_a_clean_error(monkeypatch):
    mouser = _mouser_post(monkeypatch, _Resp({}, 429))
    with pytest.raises(mouser.MouserError, match="TooManyRequests"):
        mouser.search_part("X")
    mouser = _mouser_post(monkeypatch, _Resp(
        {"Errors": [{"Code": "TooManyRequests", "Message": "Too Many Requests"}]}))
    with pytest.raises(mouser.MouserError, match="about 30 calls per"):
        mouser.search_part("X")


def test_mouser_throttles_to_thirty_calls_a_minute(monkeypatch):
    from kicad_claude.adapters import mouser
    from collections import deque
    now = [1000.0]
    slept = []
    monkeypatch.setattr(mouser.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mouser.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(mouser, "_calls", deque(990.0 + i * 0.1 for i in range(30)))
    mouser._throttle()  # 31st call within the window
    assert slept == [pytest.approx(50.0)]
    assert len(mouser._calls) == 30
    now[0] = 2000.0  # a minute later the window is empty again
    slept.clear()
    mouser._throttle()
    assert slept == [] and len(mouser._calls) == 1


def test_check_availability_prices_the_quantity(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mo_hit = {"source": "mouser", "mpn": "LM358N", "sku": "595-LM358N", "unit_price": 5.0,
              "price_breaks": [{"qty": 1, "unit_price": 5.0}, {"qty": 25, "unit_price": 3.0}]}
    mcp = _make_mcp(monkeypatch, idx, mo_results=[mo_hit])
    res = _call(mcp, "check_availability", mpn="LM358N", qty=30)
    assert res["mouser"]["sku"] == "595-LM358N"
    assert res["mouser"]["qty"] == 30 and res["mouser"]["unit_price_at_qty"] == 3.0


def test_search_distributors_ranks_by_price_at_quantity(monkeypatch):
    from kicad_claude.adapters import mouser
    seen = {}

    def fake(query, records=10):
        seen["q"], seen["n"] = query, records
        return [{"mpn": "A", "unit_price": 1.0, "price_breaks": [{"qty": 1, "unit_price": 1.0}]},
                {"mpn": "B", "unit_price": None},
                {"mpn": "C", "unit_price": 2.0, "price_breaks": [{"qty": 1, "unit_price": 2.0},
                                                                {"qty": 10, "unit_price": 0.5}]}]

    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(monkeypatch, idx)
    monkeypatch.setattr(mouser, "search_keyword", fake)
    res = _call(mcp, "search_distributors", query="22uF 10V 0805 X7R", limit=3, qty=10)
    assert seen == {"q": "22uF 10V 0805 X7R", "n": 3}
    assert [h["mpn"] for h in res["mouser"]] == ["C", "A", "B"]
    _stub_source(monkeypatch, "mouser", err="TooManyRequests")
    monkeypatch.setattr(mouser, "search_keyword", mouser.search_part)
    assert "TooManyRequests" in _call(mcp, "search_distributors", query="x")["errors"]["mouser"]


def test_enrich_bom_prices_line_quantity_times_boards(tmp_path, monkeypatch):
    write_blank_project(tmp_path / "demo", "demo")
    state.clear_active()
    state.set_active(tmp_path / "demo", "demo")
    bom = tmp_path / "bom.csv"
    bom.write_text('Reference,Value\n"R1,R2,R3",10k\nC1,1uF\n', encoding="utf-8")
    out = tmp_path / "enriched.csv"
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(monkeypatch, idx, mo_results=[{
        "source": "mouser", "mpn": "RC0402", "sku": "603-RC0402", "unit_price": 2.2,
        "price_breaks": [{"qty": 1, "unit_price": 2.2}, {"qty": 10, "unit_price": 0.6}]}])
    res = _call(mcp, "enrich_bom_with_sourcing", bom_path=str(bom),
                output_path=str(out), boards=5)
    import csv as _csv
    with out.open(encoding="utf-8", newline="") as f:
        rows = list(_csv.DictReader(f))
    assert rows[0]["mo_sku"] == "603-RC0402"
    assert rows[0]["mo_qty_price"] == "0.6" and rows[0]["mo_line_total"] == "9.0"  # 15 pcs
    assert rows[1]["mo_qty_price"] == "2.2" and rows[1]["mo_line_total"] == "11.0"  # 5 pcs
    assert res["boards"] == 5 and res["totals"] == {"mouser": 20.0}
    state.clear_active()


def test_find_or_fetch_symbol_local_hit(monkeypatch):
    # Build an index with one matching symbol
    idx = {
        "symbols": {
            "Device:R": {
                "lib_id": "Device:R",
                "lib": "Device",
                "name": "R",
                "description": "Resistor",
                "keywords": "R res resistor",
                "default_footprint": "Resistor_SMD:R_0603_1608Metric",
                "datasheet": "~",
                "pin_count": 2,
                "extends": None,
            }
        },
        "footprints": {},
        "symbol_dirs": [],
        "footprint_dirs": [],
    }
    mcp = _make_mcp(monkeypatch, idx)
    res = _call(mcp, "find_or_fetch_symbol", query="resistor")
    assert res["found"] is True
    assert res["lib_id"] == "Device:R"
    assert res["pin_count"] == 2


def test_find_or_fetch_symbol_no_match_returns_manual_instructions(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(
        monkeypatch, idx,
        dk_results=[{
            "source": "digikey", "mpn": "ACME-X1", "manufacturer": "ACME Corp",
            "stock": 0, "unit_price": 0.0,
        }],
    )
    res = _call(mcp, "find_or_fetch_symbol", query="ACME-X1", mpn="ACME-X1")
    assert res["found"] is False
    assert "ACME Corp" in res["manufacturer"]
    assert "snapeda.com" in res["snapeda_url"]
    assert "import_vendor_zip" in res["instructions"]


def test_list_vendor_parts_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sourcing_tools, "_project_root_for_vendor_parts", lambda: tmp_path
    )
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    sourcing_tools.register(mcp)
    res = _call(mcp, "list_vendor_parts")
    assert res["count"] == 0
    assert res["zips"] == []


def test_list_vendor_parts_finds_zips(tmp_path, monkeypatch):
    (tmp_path / "PARTA.zip").write_bytes(b"PK")
    (tmp_path / "PARTB.zip").write_bytes(b"PK")
    monkeypatch.setattr(
        sourcing_tools, "_project_root_for_vendor_parts", lambda: tmp_path
    )
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    sourcing_tools.register(mcp)
    res = _call(mcp, "list_vendor_parts")
    assert res["count"] == 2
    assert {z["name"] for z in res["zips"]} == {"PARTA.zip", "PARTB.zip"}


# ===== import_vendor_zip via the MCP tool (needs active project) =========== #


def test_import_zip_tool_with_active_project(tmp_path, monkeypatch):
    write_blank_project(tmp_path / "demo", "demo")
    state.clear_active()
    state.set_active(tmp_path / "demo", "demo")
    drop = tmp_path / "drops"
    drop.mkdir()
    zip_path = _make_vendor_zip(drop, name="ACME_FOO")

    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(monkeypatch, idx)
    try:
        res = _call(mcp, "import_vendor_zip", zip_path=str(zip_path), target_lib="vendor")
        assert res["symbols_added"] == 1
        assert res["footprints_added"] == 1
        assert "next_step" in res
    finally:
        state.clear_active()


# ===== TME v2 response shaping (unit) ====================================== #


def test_tme_summarize_prefers_manufacturer_symbol_over_tme_symbol():
    from kicad_claude.adapters import tme

    product = {
        "symbol": "1N4007-DIO",
        "manufacturer_symbols": ["1N4007"],
        "manufacturer": {"id": 43, "name": "DIOTEC SEMICONDUCTOR"},
        "description": "Diode: rectifying; THT; 1kV; 1A",
        "product_status": [],
    }
    price = {
        "symbol": "1N4007-DIO",
        "stock_quantity": 295573,
        "prices": {
            "elements": [
                {"amount": 1, "price": 2.94, "special": False},
                {"amount": 10, "price": 1.885, "special": False},
            ],
            "tax": {"type": "VAT", "rate": 21.0},
            "currency": "CZK",
            "type": "GROSS",
        },
    }
    out = tme._summarize_part(product, price)
    assert out["mpn"] == "1N4007"
    assert out["tme_symbol"] == "1N4007-DIO"
    assert out["stock"] == 295573
    # The 1-off break, not the cheapest one.
    assert out["unit_price"] == 2.94
    assert out["currency"] == "CZK"
    assert out["price_type"] == "GROSS"
    assert out["vat_rate"] == 21.0
    assert out["product_url"].endswith("/details/1n4007-dio/")


def test_tme_summarize_survives_missing_price_block():
    from kicad_claude.adapters import tme

    out = tme._summarize_part({"symbol": "X", "manufacturer_symbols": []}, {})
    assert out["mpn"] == "X"
    assert out["stock"] == 0
    assert out["unit_price"] is None
    # Falls back to the configured currency when the API returned none.
    assert out["currency"]


def test_tme_reuses_its_bearer_token(monkeypatch):
    """A second call must not re-run the OAuth exchange."""
    from kicad_claude.adapters import tme

    monkeypatch.setenv("TME_APP_TOKEN", "tok")
    monkeypatch.setenv("TME_APP_SECRET", "sec")
    monkeypatch.setattr(tme, "_token_cache", None)

    calls = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            calls.append(1)
            return {"access_token": f"AT{len(calls)}", "expires_in": 300}

    monkeypatch.setattr(tme.httpx, "post", lambda *a, **k: _Resp())
    assert tme.get_access_token() == "AT1"
    assert tme.get_access_token() == "AT1"
    assert len(calls) == 1


# ===== Live network tests ================================================== #


def _have_creds(*names: str) -> bool:
    return all(os.environ.get(n) for n in names)


@pytest.mark.network
@pytest.mark.skipif(
    not _have_creds("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"),
    reason="DigiKey credentials missing",
)
def test_digikey_live_lookup_lm358n():
    from kicad_claude.adapters import digikey
    results = digikey.search_keyword("LM358N", limit=3)
    assert results, "expected at least one result for LM358N"
    assert all(r.get("mpn") for r in results)


@pytest.mark.network
@pytest.mark.skipif(
    not _have_creds("MOUSER_API_KEY"),
    reason="Mouser API key missing",
)
def test_mouser_live_lookup_lm358n():
    from kicad_claude.adapters import mouser
    # If the key is wrong (e.g., Order key vs Search key), the adapter raises.
    # We don't fail the suite on that — just record the issue clearly.
    try:
        results = mouser.search_part("LM358N")
    except mouser.MouserError as e:
        pytest.skip(f"Mouser API key rejected: {e}")
    assert results, "expected at least one result for LM358N"


@pytest.mark.network
@pytest.mark.skipif(
    not _have_creds("TME_APP_TOKEN", "TME_APP_SECRET"),
    reason="TME credentials missing",
)
def test_tme_live_lookup_lm358n():
    from kicad_claude.adapters import tme
    try:
        results = tme.search_part("LM358N")
    except tme.TMEError as e:
        pytest.skip(f"TME API rejected the request: {e}")
    assert results, "expected at least one result for LM358N"
    assert all(r.get("mpn") for r in results)


@pytest.mark.network
@pytest.mark.skipif(
    not _have_creds("FARNELL_API_KEY"),
    reason="Farnell API key missing",
)
def test_farnell_live_lookup_lm358n():
    from kicad_claude.adapters import farnell
    try:
        results = farnell.search_part("LM358N")
    except farnell.FarnellError as e:
        pytest.skip(f"Farnell API rejected the request: {e}")
    assert results, "expected at least one result for LM358N"
    assert all(r.get("mpn") for r in results)


# ===== jlcpcb source ======================================================= #

_JLC_PAYLOAD = {
    "code": 200,
    "data": {"componentPageInfo": {"list": [
        {"componentCode": "C99999", "componentModelEn": "OTHER-PART",
         "componentBrandEn": "X", "stockCount": 5, "componentLibraryType": "expand",
         "preferredComponentFlag": True, "componentPrices": [],
         "urlSuffix": "X-OTHER/C99999"},
        {"componentCode": "C51118", "componentModelEn": "AP2112K-3.3TRG1",
         "componentBrandEn": "Diodes Incorporated", "stockCount": 13262,
         "componentLibraryType": "base", "preferredComponentFlag": False,
         "componentSpecificationEn": "SOT-25-5", "describe": "LDO 3.3V",
         "componentPrices": [{"startNumber": 10, "endNumber": 29, "productPrice": 0.15},
                             {"startNumber": 1, "endNumber": 9, "productPrice": 0.18}],
         "dataManualUrl": "https://www.lcsc.com/datasheet/x.pdf",
         "urlSuffix": "DiodesIncorporated-AP2112K_33TRG1/C51118"},
    ]}},
}


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, str(payload)

    def json(self):  # noqa: F811 — httpx.Response API, not the json module
        return self._p


def test_jlcpcb_adapter_summarizes_and_ranks(monkeypatch):
    from kicad_claude.adapters import jlcpcb
    seen = {}

    def fake_post(url, **kw):
        seen["url"], seen["body"] = url, kw["json"]
        return _Resp(_JLC_PAYLOAD)

    monkeypatch.setattr(jlcpcb.httpx, "post", fake_post)
    rows = jlcpcb.search_part("AP2112K-3.3TRG1")
    assert seen["body"]["keyword"] == "AP2112K-3.3TRG1"
    top = rows[0]
    assert top["mpn"] == "AP2112K-3.3TRG1" and top["lcsc"] == "C51118"
    assert top["library_type"] == "basic" and top["unit_price"] == 0.18
    assert [b["qty"] for b in top["price_breaks"]] == [1, 10]
    assert top["product_url"].endswith("/C51118") and top["currency"] == "USD"
    assert rows[1]["library_type"] == "preferred"
    assert jlcpcb.search_part("C51118")[0]["lcsc"] == "C51118"  # by LCSC number


def test_jlcpcb_errors_are_per_source(monkeypatch):
    from kicad_claude.adapters import jlcpcb
    monkeypatch.setattr(jlcpcb.httpx, "post", lambda *a, **k: _Resp({}, 503))
    with pytest.raises(jlcpcb.JLCPCBError):
        jlcpcb.search_part("X")
    monkeypatch.setattr(jlcpcb.httpx, "post",
                        lambda *a, **k: _Resp({"code": 500, "message": "boom"}))
    with pytest.raises(jlcpcb.JLCPCBError, match="boom"):
        jlcpcb.search_part("X")
    assert "jlcpcb" in sourcing_tools.SOURCE_NAMES
    assert sourcing_tools._parse_sources(["jlcpcb", "mouser"]) == ["mouser", "jlcpcb"]
    assert "jlcpcb" not in sourcing_tools.DEFAULT_SOURCES


@pytest.mark.network
def test_jlcpcb_live_finds_an_rf_module():
    from kicad_claude.adapters import jlcpcb
    hit = jlcpcb.search_part("ESP32-C6-MINI-1-N4")[0]
    assert hit["mpn"] == "ESP32-C6-MINI-1-N4" and hit["lcsc"].startswith("C")


# ===== fetch_datasheet ===================================================== #


def _mock_stream(monkeypatch, routes):
    import httpx
    from kicad_claude.adapters import datasheet_fetch

    def handler(request):
        kind = routes[str(request.url)]
        if kind == "pdf":
            return httpx.Response(200, content=b"%PDF-1.7\n" + b"x" * 4000)
        if kind == "html":
            return httpx.Response(200, content=b"<html><body>Are you a robot?</body></html>" * 50)
        if kind == "cert":
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired")
        if kind.startswith("redirect:"):
            return httpx.Response(302, headers={"location": kind[len("redirect:"):]})
        return httpx.Response(int(kind))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(datasheet_fetch.httpx, "stream",
                        lambda method, url, **kw: client.stream(method, url))
    # `.example` hosts do not resolve; give them a public address.
    monkeypatch.setattr(datasheet_fetch, "_host_addresses", lambda host: ["93.184.216.34"])


def test_fetch_first_pdf_falls_back(monkeypatch, tmp_path):
    from kicad_claude.adapters import datasheet_fetch
    _mock_stream(monkeypatch, {
        "https://onsemi.example/a.pdf": "cert",
        "https://littelfuse.example/b.pdf": "html",
        "https://mouser.example/c.pdf": "403",
        "https://farnell.example/ds/SMF24A.pdf": "pdf",
    })
    cands = [{"source": "url", "url": "https://onsemi.example/a.pdf"},
             {"source": "digikey", "url": "https://littelfuse.example/b.pdf"},
             {"source": "mouser", "url": "https://mouser.example/c.pdf"},
             {"source": "farnell", "url": "https://farnell.example/ds/SMF24A.pdf"}]
    r = datasheet_fetch.fetch_first_pdf(cands, "SMF24A", tmp_path)
    assert r["saved"] and r["source"] == "farnell"
    assert (tmp_path / "SMF24A.pdf").read_bytes().startswith(b"%PDF")
    reasons = [a["reason"] for a in r["attempts"]]
    assert "TLS certificate error" in reasons[0] and "HTML" in reasons[1]
    assert reasons[2] == "HTTP 403"
    # Second call keeps the file.
    r2 = datasheet_fetch.fetch_first_pdf(cands[3:], "SMF24A", tmp_path)
    assert r2["saved"] is False and r2["existing"] is True


def test_fetch_datasheet_tool_orders_sources(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP
    from kicad_claude.adapters import datasheet_fetch

    mcp = FastMCP("t")
    sourcing_tools.register(mcp)
    _stub_source(monkeypatch, "mouser", [{"mpn": "P1", "datasheet_url": "https://m.example/p1.pdf"}])
    _stub_source(monkeypatch, "farnell", [{"mpn": "P1", "datasheet_url": "https://f.example/p1.pdf"}])
    got = {}

    def fake(cands, mpn, dest, overwrite=False):
        got["cands"] = cands
        return {"saved": False, "path": None, "source": None, "url": None, "attempts": []}

    monkeypatch.setattr(datasheet_fetch, "fetch_first_pdf", fake)
    r = mcp._tool_manager.get_tool("fetch_datasheet").fn(
        mpn="P1", sources=["farnell", "mouser"], url="https://x.example/p1.pdf", dest_dir=str(tmp_path))
    assert [c["source"] for c in got["cands"]] == ["url", "farnell", "mouser"]
    assert "hint" in r


# ===== datasheet download: SSRF guard (OP-16) ============================== #


@pytest.mark.parametrize("url,reason", [
    ("http://127.0.0.1/ds.pdf", "non-public"),
    ("http://localhost:8080/ds.pdf", "non-public"),
    ("http://10.0.0.5/ds.pdf", "non-public"),
    ("http://192.168.1.1/ds.pdf", "non-public"),
    ("http://169.254.169.254/latest/meta-data", "non-public"),
    ("http://[::1]/ds.pdf", "non-public"),
    ("file:///etc/passwd", "not an http(s) URL"),
    ("ftp://ftp.example/ds.pdf", "not an http(s) URL"),
    ("http://user:pw@vendor.example/ds.pdf", "credentials"),
    ("http://vendor.example\@127.0.0.1/ds.pdf", "malformed host"),
])
def test_download_refuses_non_public_and_odd_urls(monkeypatch, url, reason):
    from kicad_claude.adapters import datasheet_fetch

    def resolve(host):
        return {"localhost": ["127.0.0.1", "::1"]}.get(host, ["93.184.216.34"])

    monkeypatch.setattr(datasheet_fetch, "_host_addresses", resolve)
    monkeypatch.delenv(datasheet_fetch.ALLOW_PRIVATE_ENV, raising=False)
    body, why = datasheet_fetch.try_download(url)
    assert body is None and reason in why


def test_redirect_to_a_private_address_is_refused(monkeypatch):
    from kicad_claude.adapters import datasheet_fetch
    _mock_stream(monkeypatch, {"https://vendor.example/ds.pdf": "redirect:http://127.0.0.1/x.pdf"})
    body, why = datasheet_fetch.try_download("https://vendor.example/ds.pdf")
    assert body is None and "non-public" in why


def test_redirects_are_followed_to_a_public_pdf(monkeypatch):
    from kicad_claude.adapters import datasheet_fetch
    _mock_stream(monkeypatch, {
        "https://vendor.example/ds": "redirect:/files/ds.pdf",
        "https://vendor.example/files/ds.pdf": "pdf",
    })
    body, why = datasheet_fetch.try_download("https://vendor.example/ds")
    assert why == "" and body.startswith(b"%PDF")


def test_redirect_loop_stops(monkeypatch):
    from kicad_claude.adapters import datasheet_fetch
    _mock_stream(monkeypatch, {"https://vendor.example/a": "redirect:https://vendor.example/a"})
    body, why = datasheet_fetch.try_download("https://vendor.example/a")
    assert body is None and "redirects" in why


def test_private_hosts_need_the_explicit_opt_in(monkeypatch):
    from kicad_claude.adapters import datasheet_fetch
    monkeypatch.setenv(datasheet_fetch.ALLOW_PRIVATE_ENV, "1")
    assert datasheet_fetch.check_url("http://10.0.0.5/ds.pdf") == ""


# ===== SKU choice and minimum order (OP-44) ================================ #

_REEL = {"source": "farnell", "mpn": "USBLC6-2SC6", "sku": "2473979", "stock": 0,
         "unit_price": 8.147, "min_qty": 3000,
         "price_breaks": [{"qty": 3000, "unit_price": 8.147}]}
_CUT = {"source": "farnell", "mpn": "USBLC6-2SC6", "sku": "1269406", "stock": 28982,
        "unit_price": 13.23, "min_qty": 5,
        "price_breaks": [{"qty": 5, "unit_price": 13.23}, {"qty": 50, "unit_price": 9.047}]}
_RE_REEL = {**_CUT, "sku": "1269406RL", "min_qty": 100}


def test_buy_qty_honours_minimum_order_and_multiple():
    assert sourcing_tools.buy_qty(_CUT, 1) == 5
    assert sourcing_tools.buy_qty(_CUT, 7) == 7
    assert sourcing_tools.buy_qty(_REEL, 1) == 3000
    # no min_qty: the first price break is the minimum
    assert sourcing_tools.buy_qty({"price_breaks": [{"qty": 10, "unit_price": 1.0}]}, 3) == 10
    assert sourcing_tools.buy_qty({"min_qty": 1, "order_multiple": 4}, 5) == 8
    assert sourcing_tools.buy_qty({}, 2) == 2


def test_check_availability_picks_the_in_stock_sku_and_prices_the_minimum(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(monkeypatch, idx, fn_results=[_REEL, _RE_REEL, _CUT])
    res = _call(mcp, "check_availability", mpn="USBLC6-2SC6", sources=["farnell"])
    fn = res["farnell"]
    assert fn["sku"] == "1269406"
    assert fn["buy_qty"] == 5 and fn["unit_price_at_qty"] == 13.23
    assert fn["line_total"] == round(5 * 13.23, 4)


def test_search_distributors_ranks_by_order_cost(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(monkeypatch, idx)
    monkeypatch.setattr(sourcing_tools.farnell, "search_keyword",
                        lambda *a, **k: [_REEL, _RE_REEL, _CUT])
    res = _call(mcp, "search_distributors", query="USBLC6", sources=["farnell"])
    assert [h["sku"] for h in res["farnell"]][:2] == ["1269406", "1269406RL"]


def test_enrich_bom_buys_the_minimum_order(tmp_path, monkeypatch):
    write_blank_project(tmp_path / "demo", "demo")
    state.clear_active()
    state.set_active(tmp_path / "demo", "demo")
    bom = tmp_path / "bom.csv"
    bom.write_text("Reference,MPN,Qty\nU6,USBLC6-2SC6,1\n", encoding="utf-8")
    out = tmp_path / "enriched.csv"
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(monkeypatch, idx, fn_results=[_REEL, _CUT])
    res = _call(mcp, "enrich_bom_with_sourcing", bom_path=str(bom), output_path=str(out),
                sourcing_field="MPN", sources=["farnell"])
    import csv as _csv
    with out.open(encoding="utf-8", newline="") as f:
        row = next(_csv.DictReader(f))
    assert row["fn_sku"] == "1269406" and row["fn_buy_qty"] == "5"
    assert res["totals"]["farnell"] == round(5 * 13.23, 2)
    state.clear_active()


def test_adapters_report_minimum_order():
    from kicad_claude.adapters import farnell, mouser
    fn = farnell._summarize_part({"sku": "1", "translatedMinimumOrderQuality": 5,
                                  "prices": [{"from": 5, "cost": 13.23}]})
    assert fn["min_qty"] == 5
    mo = mouser._summarize_part({"Min": "10", "Mult": "10", "PriceBreaks": []})
    assert mo["min_qty"] == 10 and mo["order_multiple"] == 10
    assert mouser._summarize_part({})["min_qty"] is None
