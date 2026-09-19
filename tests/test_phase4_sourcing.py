"""Phase 4 — sourcing.

Strategy:
- Unit tests for vendor_import (build synthetic ZIPs at runtime, no network).
- Tool layer: monkeypatch the distributor adapters so tests run offline.
- @pytest.mark.network: real DigiKey / Mouser / TME / Farnell calls.
  Skipped if the matching credentials are absent.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest
from dotenv import load_dotenv

from kicad_claude import state
from kicad_claude.adapters import vendor_import
from kicad_claude.adapters.snapeda import manual_fallback_message, part_page_url
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import sourcing as sourcing_tools


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


#: adapter attribute each source is queried through, per `_search_source`.
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
        exc = sourcing_tools._source_error(name)

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
    res = _call(mcp, "check_availability", mpn="LM358N", sources="digikey,mouser")
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
    res = _call(mcp, "check_availability", mpn="LM358N", sources="digikey,mouser")
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
    res = _call(mcp, "check_availability", mpn="LM358N", sources="tme,farnell")
    assert res["sources"] == ["tme", "farnell"]
    assert res["tme"]["stock"] == 42
    assert res["farnell"]["stock"] == 17
    assert res["errors"] == {}


def test_check_availability_tme_failure_surfaces_error(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(monkeypatch, idx, tme_err="bad signature")
    res = _call(mcp, "check_availability", mpn="LM358N", sources="tme")
    assert res["tme"] is None
    assert "bad signature" in res["errors"]["tme"]


def test_check_availability_rejects_unknown_source(monkeypatch):
    idx = {"symbols": {}, "footprints": {}, "symbol_dirs": [], "footprint_dirs": []}
    mcp = _make_mcp(monkeypatch, idx)
    with pytest.raises(ValueError, match="Unknown source"):
        _call(mcp, "check_availability", mpn="LM358N", sources="rs-online")


def test_parse_sources_orders_and_deduplicates():
    assert sourcing_tools._parse_sources("farnell, TME ,mouser") == [
        "mouser", "tme", "farnell"
    ]
    with pytest.raises(ValueError, match="No sources"):
        sourcing_tools._parse_sources("  ")


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
        bom_path=str(bom), output_path=str(out), sources="tme,farnell",
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
    files = write_blank_project(tmp_path / "demo", "demo")
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
