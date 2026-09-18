"""Phase 2 — library indexer + search tools.

Fast unit tests use the synthetic fixtures in tests/fixtures/. The end-to-end
test that indexes the full KiCAD install is marked @pytest.mark.slow and can
be skipped with `pytest -m "not slow"`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_claude.indexer import kicad_libs as klibs
from kicad_claude.indexer.search import search_footprints, search_symbols
from kicad_claude.tools import library as lib_tools

FIXTURES = Path(__file__).parent / "fixtures"


# ----- Symbol lib parsing -------------------------------------------------- #


def test_parse_symbol_lib_extracts_symbols_and_metadata():
    syms = klibs.parse_symbol_lib(FIXTURES / "MiniLib.kicad_sym")
    by_lib_id = {s["lib_id"]: s for s in syms}
    assert "MiniLib:Resistor" in by_lib_id
    r = by_lib_id["MiniLib:Resistor"]
    assert r["description"] == "Generic resistor"
    assert r["keywords"] == "R res resistor"
    assert r["default_footprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert r["pin_count"] == 2


def test_parse_symbol_lib_resolves_extends_for_pin_count():
    syms = klibs.parse_symbol_lib(FIXTURES / "MiniLib.kicad_sym")
    by_lib_id = {s["lib_id"]: s for s in syms}
    small = by_lib_id["MiniLib:R_Small"]
    assert small["extends"] == "Resistor"
    # Inherits pin_count from base Resistor
    assert small["pin_count"] == 2


def test_parse_symbol_lib_counts_multi_pin_symbols():
    syms = klibs.parse_symbol_lib(FIXTURES / "MiniLib.kicad_sym")
    by_lib_id = {s["lib_id"]: s for s in syms}
    assert by_lib_id["MiniLib:ESP32_Demo"]["pin_count"] == 3


def test_get_symbol_pins_returns_pin_metadata():
    pins = klibs.get_symbol_pins(FIXTURES / "MiniLib.kicad_sym", "ESP32_Demo")
    assert len(pins) == 3
    by_number = {p["number"]: p for p in pins}
    assert by_number["1"]["name"] == "VCC"
    assert by_number["1"]["type"] == "power_in"
    assert by_number["3"]["name"] == "GPIO0"
    assert by_number["3"]["type"] == "bidirectional"


# ----- Footprint parsing --------------------------------------------------- #


def test_parse_footprint_extracts_descr_and_tags():
    fp_path = FIXTURES / "MiniFP.pretty" / "Mini_R_0603.kicad_mod"
    fp = klibs.parse_footprint(fp_path, "MiniFP")
    assert fp["lib_id"] == "MiniFP:Mini_R_0603"
    assert fp["description"] == "Tiny test resistor footprint 0603"
    assert fp["tags"] == "resistor smd 0603"


# ----- build_index end-to-end on fixtures ---------------------------------- #


def test_build_index_with_fixture_dirs(tmp_path: Path):
    # Copy MiniLib.kicad_sym into a temp symbol dir
    sym_dir = tmp_path / "syms"
    sym_dir.mkdir()
    shutil.copy(FIXTURES / "MiniLib.kicad_sym", sym_dir / "MiniLib.kicad_sym")
    fp_dir = tmp_path / "fps"
    fp_dir.mkdir()
    shutil.copytree(FIXTURES / "MiniFP.pretty", fp_dir / "MiniFP.pretty")

    idx = klibs.build_index(symbol_dirs=[sym_dir], footprint_dirs=[fp_dir])
    assert idx["version"] == klibs.INDEX_VERSION
    assert "MiniLib:Resistor" in idx["symbols"]
    assert "MiniFP:Mini_R_0603" in idx["footprints"]
    assert idx["symbol_dirs"] == [str(sym_dir)]


# ----- Search -------------------------------------------------------------- #


@pytest.fixture
def synthetic_index(tmp_path):
    sym_dir = tmp_path / "syms"
    sym_dir.mkdir()
    shutil.copy(FIXTURES / "MiniLib.kicad_sym", sym_dir / "MiniLib.kicad_sym")
    fp_dir = tmp_path / "fps"
    fp_dir.mkdir()
    shutil.copytree(FIXTURES / "MiniFP.pretty", fp_dir / "MiniFP.pretty")
    return klibs.build_index(symbol_dirs=[sym_dir], footprint_dirs=[fp_dir])


def test_search_symbols_finds_by_name(synthetic_index):
    results = search_symbols("ESP32", synthetic_index)
    assert results
    assert results[0]["lib_id"] == "MiniLib:ESP32_Demo"


def test_search_symbols_finds_by_keyword(synthetic_index):
    results = search_symbols("wifi", synthetic_index, score_cutoff=50)
    assert any(r["lib_id"] == "MiniLib:ESP32_Demo" for r in results)


def test_search_footprints_by_tag(synthetic_index):
    results = search_footprints("0603", synthetic_index)
    assert results
    assert results[0]["lib_id"] == "MiniFP:Mini_R_0603"


def test_search_empty_query_returns_empty(synthetic_index):
    assert search_symbols("", synthetic_index) == []
    assert search_footprints("   ", synthetic_index) == []


# ----- Tools layer (via FastMCP) ------------------------------------------- #


def _make_mcp_with_lib_tools(monkeypatch, idx):
    """Patch the cache loader to return our synthetic index, then register tools."""
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)
    mcp = FastMCP("test")
    lib_tools.register(mcp)
    return mcp


def _call(mcp, name, **kwargs):
    return mcp._tool_manager.get_tool(name).fn(**kwargs)


def test_index_libraries_returns_summary_from_cache(synthetic_index, monkeypatch):
    mcp = _make_mcp_with_lib_tools(monkeypatch, synthetic_index)
    res = _call(mcp, "index_libraries")
    assert res["from_cache"] is True
    assert res["symbols"] == len(synthetic_index["symbols"])
    assert res["footprints"] == len(synthetic_index["footprints"])


def test_list_libraries_returns_per_lib_counts(synthetic_index, monkeypatch):
    mcp = _make_mcp_with_lib_tools(monkeypatch, synthetic_index)
    _call(mcp, "index_libraries")  # warm in-process cache
    res = _call(mcp, "list_libraries")
    assert {"name": "MiniLib", "count": 4} in res["symbol_libraries"]
    assert {"name": "MiniFP", "count": 1} in res["footprint_libraries"]


def test_search_symbol_tool_returns_ranked_matches(synthetic_index, monkeypatch):
    mcp = _make_mcp_with_lib_tools(monkeypatch, synthetic_index)
    _call(mcp, "index_libraries")
    res = _call(mcp, "search_symbol", query="esp32-s3")
    assert res
    assert res[0]["lib_id"] == "MiniLib:ESP32_Demo"
    assert "_score" in res[0]


def test_get_symbol_details_includes_pins(synthetic_index, monkeypatch):
    mcp = _make_mcp_with_lib_tools(monkeypatch, synthetic_index)
    _call(mcp, "index_libraries")
    details = _call(mcp, "get_symbol_details", lib_id="MiniLib:ESP32_Demo")
    assert details["pin_count"] == 3
    assert len(details["pins"]) == 3
    assert {p["name"] for p in details["pins"]} == {"VCC", "GND", "GPIO0"}


def test_get_symbol_details_unknown_id_raises(synthetic_index, monkeypatch):
    mcp = _make_mcp_with_lib_tools(monkeypatch, synthetic_index)
    _call(mcp, "index_libraries")
    with pytest.raises(KeyError):
        _call(mcp, "get_symbol_details", lib_id="DoesNot:Exist")


def test_tool_without_index_raises(monkeypatch):
    """If no index has been built, the search/get tools refuse."""
    monkeypatch.setattr(lib_tools, "load_cache", lambda: None)
    monkeypatch.setattr(lib_tools, "_index", None)
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    lib_tools.register(mcp)
    with pytest.raises(RuntimeError, match="not built"):
        _call(mcp, "search_symbol", query="anything")


# ----- Slow: real KiCAD install -------------------------------------------- #


@pytest.mark.slow
def test_full_kicad_indexing_meets_acceptance():
    """Acceptance test: real KiCAD install indexes 100+ libs and finds ESP32-S3."""
    from kicad_claude.utils.kicad_paths import find_symbol_lib_dirs

    if not find_symbol_lib_dirs():
        pytest.skip("no KiCAD symbol libraries installed")

    idx = klibs.build_index()
    assert len(idx["symbols"]) > 1000, "expected >1k symbols indexed"
    assert len(idx["footprints"]) > 1000, "expected >1k footprints indexed"

    results = search_symbols("ESP32-S3", idx, max_results=20)
    assert any("ESP32" in r["lib_id"] for r in results), \
        f"expected ESP32 match, got {[r['lib_id'] for r in results[:5]]}"


# --------------------------------------------------------------------------- #
# P7 — search results decode prose, not identity
# --------------------------------------------------------------------------- #


class TestSearchBraceDecoding:
    """`{brace}` escapes are decoded for reading, never for `lib_id`."""

    @staticmethod
    def _index_with_escapes():
        return {
            "symbols": {
                "MiniLib:Bridge{slash}Rect": {
                    "lib_id": "MiniLib:Bridge{slash}Rect",
                    "lib": "MiniLib",
                    "name": "Bridge{slash}Rect",
                    "description": "Bridge rectifier, AC{slash}DC input",
                    "keywords": "diode bridge ac{slash}dc",
                    "default_footprint": "Diode_THT:D{slash}Bridge",
                    "datasheet": "~",
                    "pin_count": 4,
                    "extends": None,
                }
            },
            "footprints": {
                "MiniFP:Pad{slash}Big": {
                    "lib_id": "MiniFP:Pad{slash}Big",
                    "lib": "MiniFP",
                    "name": "Pad{slash}Big",
                    "description": "Big pad, 2{slash}1 aspect",
                    "tags": "pad big 2{slash}1",
                }
            },
            "symbol_dirs": [],
            "footprint_dirs": [],
        }

    def _mcp(self, monkeypatch):
        from mcp.server.fastmcp import FastMCP

        idx = self._index_with_escapes()
        monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
        monkeypatch.setattr(lib_tools, "_index", None)
        mcp = FastMCP("test")
        lib_tools.register(mcp)
        return mcp

    def test_symbol_description_and_keywords_are_decoded(self, monkeypatch):
        mcp = self._mcp(monkeypatch)
        hit = _call(mcp, "search_symbol", query="bridge rectifier")[0]
        assert hit["description"] == "Bridge rectifier, AC/DC input"
        assert hit["keywords"] == "diode bridge ac/dc"

    def test_symbol_identity_is_left_alone(self, monkeypatch):
        """lib_id goes straight back into add_symbol — decoding it breaks lookup."""
        mcp = self._mcp(monkeypatch)
        hit = _call(mcp, "search_symbol", query="bridge rectifier")[0]
        assert hit["lib_id"] == "MiniLib:Bridge{slash}Rect"
        assert hit["name"] == "Bridge{slash}Rect"

    def test_footprint_description_and_tags_are_decoded(self, monkeypatch):
        mcp = self._mcp(monkeypatch)
        hit = _call(mcp, "search_footprint", query="big pad")[0]
        assert hit["description"] == "Big pad, 2/1 aspect"
        assert hit["tags"] == "pad big 2/1"
        assert hit["lib_id"] == "MiniFP:Pad{slash}Big"

    def test_get_symbol_details_decodes_prose_only(self, monkeypatch):
        mcp = self._mcp(monkeypatch)
        res = _call(mcp, "get_symbol_details", lib_id="MiniLib:Bridge{slash}Rect")
        assert res["description"] == "Bridge rectifier, AC/DC input"
        assert res["default_footprint"] == "Diode_THT:D/Bridge"
        assert res["lib_id"] == "MiniLib:Bridge{slash}Rect"

    def test_plain_entries_are_unchanged(self, monkeypatch):
        """No braces, no difference — the common case must not shift."""
        from mcp.server.fastmcp import FastMCP

        idx = klibs.build_index(symbol_dirs=[FIXTURES], footprint_dirs=[FIXTURES])
        monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
        monkeypatch.setattr(lib_tools, "_index", None)
        mcp = FastMCP("test")
        lib_tools.register(mcp)

        hits = _call(mcp, "search_symbol", query="resistor")
        assert hits
        for hit in hits:
            assert "{" not in hit["lib_id"]
