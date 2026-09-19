"""Phase 19 — refreshing a sheet's cached `(lib_symbols ...)`.

Each sheet carries its own copy of every library part it places, so it opens
without the libraries. Nothing used to update that copy: a library edited on
disk never reached the sheet, and — because `inject_lib_symbol` returns early
on a known `lib_id` — not even a *newly placed* symbol picked up the change.
Everything downstream reads the cache, so `list_sch_nets`, `find_dangling` and
`get_pin_position` all reported the stale geometry.

The fixture library is copied into `tmp_path` so the tests can edit it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import schematic as sch_tools

FIXTURES = Path(__file__).parent / "fixtures"
MINILIB = FIXTURES / "MiniLib.kicad_sym"

# Pin 1 of MiniLib:Resistor, and where the edit below moves it.
PIN1_ORIGINAL = "(at 0 2.54 270)"
PIN1_MOVED = "(at 0 7.62 270)"


@pytest.fixture
def lib_dir(tmp_path: Path) -> Path:
    """A writable copy of the fixture library."""
    d = tmp_path / "libs"
    d.mkdir()
    shutil.copyfile(MINILIB, d / "MiniLib.kicad_sym")
    return d


@pytest.fixture
def project(tmp_path: Path, lib_dir: Path, monkeypatch):
    """An active blank project whose only indexed library is `lib_dir`."""
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")

    def reindex():
        idx = kicad_libs.build_index(symbol_dirs=[lib_dir], footprint_dirs=[])
        monkeypatch.setattr(lib_tools, "load_cache", lambda: idx, raising=False)
        monkeypatch.setattr(lib_tools, "_index", None, raising=False)
        return idx

    reindex()
    files["reindex"] = reindex
    yield files
    state.clear_active()


@pytest.fixture
def mcp(project):
    from mcp.server.fastmcp import FastMCP

    m = FastMCP("test")
    sch_tools.register(m)
    lib_tools.register(m)
    return m


def _call(mcp, tool_name, /, **kwargs):
    return mcp._tool_manager.get_tool(tool_name).fn(**kwargs)


def _move_pin1(lib_dir: Path) -> None:
    lib = lib_dir / "MiniLib.kicad_sym"
    text = lib.read_text(encoding="utf-8")
    assert PIN1_ORIGINAL in text
    lib.write_text(text.replace(PIN1_ORIGINAL, PIN1_MOVED, 1), encoding="utf-8")


def _place_r1(mcp, wired: bool = False) -> dict[str, list]:
    _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1",
          value="10k", x_mm=100.33, y_mm=100.33)
    pins = {p["number"]: p["position_mm"] for p in _call(mcp, "list_pins", reference="R1")}
    if wired:
        for px, py in pins.values():
            _call(mcp, "add_wire", x1_mm=px, y1_mm=py, x2_mm=px + 25.4, y2_mm=py,
                  snap_to_grid=False)
    return pins


# ===== The staleness itself ================================================ #


class TestStaleCache:
    def test_a_library_edit_does_not_reach_a_placed_symbol(
        self, project, mcp, lib_dir
    ):
        before = _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        after = {p["number"]: p["position_mm"] for p in _call(mcp, "list_pins", reference="R1")}
        assert after == before, "the sheet caches its own copy — this is expected"

    def test_a_newly_placed_symbol_is_stale_too(self, project, mcp, lib_dir):
        """The surprising half: the cache short-circuits the injection."""
        before = _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R2",
              value="1k", x_mm=150.0, y_mm=100.33)
        r2 = {p["number"]: p["position_mm"] for p in _call(mcp, "list_pins", reference="R2")}
        assert r2["1"][1] - 100.33 == before["1"][1] - 100.33


# ===== list_lib_symbols ==================================================== #


class TestListLibSymbols:
    def test_a_fresh_sheet_reports_nothing_stale(self, project, mcp):
        _place_r1(mcp)
        entries = _call(mcp, "list_lib_symbols")["symbols"]
        assert [e["lib_id"] for e in entries] == ["MiniLib:Resistor"]
        assert entries[0]["stale"] is False

    def test_an_edited_library_shows_up_as_stale(self, project, mcp, lib_dir):
        _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        entry = _call(mcp, "list_lib_symbols")["symbols"][0]
        assert entry["stale"] is True
        assert entry["pins_moved"] == ["1"]
        assert entry["pins_added"] == [] and entry["pins_removed"] == []

    def test_a_part_missing_from_the_libraries_reports_its_error(
        self, project, mcp, lib_dir
    ):
        _place_r1(mcp)
        (lib_dir / "MiniLib.kicad_sym").unlink()
        project["reindex"]()
        entry = _call(mcp, "list_lib_symbols")["symbols"][0]
        assert entry["stale"] is None
        assert "error" in entry

    def test_bad_scope_rejected(self, project, mcp):
        with pytest.raises(ValueError, match="scope must be"):
            _call(mcp, "list_lib_symbols", scope="sideways")


# ===== refresh_lib_symbols ================================================= #


class TestRefreshDryRun:
    def test_dry_run_is_the_default_and_writes_nothing(self, project, mcp, lib_dir):
        _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        sch = Path(project["sch"])
        before = sch.read_bytes()

        res = _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor")
        assert res["dry_run"] is True
        assert res["backups"] == []
        assert sch.read_bytes() == before

    def test_dry_run_reports_what_would_move(self, project, mcp, lib_dir):
        _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        report = _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor")["reports"][0]
        assert report["changed"] is True
        assert report["pins_moved"] == ["1"]

    def test_connections_at_risk_name_the_pin_and_what_is_on_it(
        self, project, mcp, lib_dir
    ):
        _place_r1(mcp, wired=True)
        _move_pin1(lib_dir)
        project["reindex"]()
        report = _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor")["reports"][0]
        at_risk = report["instances"][0]["connections_at_risk"]
        assert [a["pin"] for a in at_risk] == ["1"]
        assert at_risk[0]["reason"] == "moved"
        assert "wire" in at_risk[0]["items"]

    def test_an_unwired_pin_is_not_at_risk(self, project, mcp, lib_dir):
        _place_r1(mcp, wired=False)
        _move_pin1(lib_dir)
        project["reindex"]()
        report = _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor")["reports"][0]
        assert report["instances"][0]["connections_at_risk"] == []

    def test_nothing_to_do_reports_unchanged(self, project, mcp):
        _place_r1(mcp)
        res = _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor")
        assert res["changed"] == 0
        assert res["reports"][0]["changed"] is False


class TestRefreshApplied:
    def test_the_cached_definition_is_replaced(self, project, mcp, lib_dir):
        before = _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor", dry_run=False)

        after = {p["number"]: p["position_mm"] for p in _call(mcp, "list_pins", reference="R1")}
        assert after["1"] != before["1"]
        assert after["2"] == before["2"]
        assert _call(mcp, "list_lib_symbols")["symbols"][0]["stale"] is False

    def test_applying_writes_a_backup(self, project, mcp, lib_dir):
        _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        res = _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor", dry_run=False)
        assert len(res["backups"]) == 1
        assert Path(res["backups"][0]).is_file()

    def test_an_unchanged_sheet_is_not_rewritten(self, project, mcp):
        _place_r1(mcp)
        sch = Path(project["sch"])
        before = sch.read_bytes()
        res = _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor", dry_run=False)
        assert res["backups"] == []
        assert sch.read_bytes() == before

    def test_a_moved_pin_leaves_its_wire_behind_and_find_dangling_says_so(
        self, project, mcp, lib_dir
    ):
        """The documented consequence — it is reported, not hidden."""
        pins = _place_r1(mcp, wired=True)
        old_pin1 = pins["1"]
        _move_pin1(lib_dir)
        project["reindex"]()

        def orphaned_wire_ends():
            return [
                d["position_mm"]
                for d in _call(mcp, "find_dangling")["items"]
                if d["kind"] == "wire_end"
            ]

        # Before: the wire still reaches pin 1, so no wire is orphaned.
        assert orphaned_wire_ends() == []

        _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor", dry_run=False)

        # After: the pin moved, the wire did not, and it now touches nothing.
        assert old_pin1 in orphaned_wire_ends()

    def test_refreshing_every_part_needs_no_lib_id(self, project, mcp, lib_dir):
        _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        res = _call(mcp, "refresh_lib_symbols", dry_run=False)
        assert res["checked"] == 1
        assert res["changed"] == 1

    def test_an_uncached_lib_id_raises(self, project, mcp):
        _place_r1(mcp)
        with pytest.raises(KeyError, match="not cached"):
            _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:ESP32_Demo")

    def test_the_entry_keeps_its_place_in_the_block(self, project, mcp, lib_dir):
        """Replacing in place, so the file's ordering does not churn."""
        _place_r1(mcp)
        _call(mcp, "add_symbol", lib_id="MiniLib:ESP32_Demo", reference="U1",
              value="ESP32", x_mm=60.0, y_mm=60.0)
        before = ed.lib_symbol_ids(sch_io.parse_file(project["sch"]))

        _move_pin1(lib_dir)
        project["reindex"]()
        _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor", dry_run=False)

        assert ed.lib_symbol_ids(sch_io.parse_file(project["sch"])) == before


class TestFieldUpdates:
    def test_nothing_is_copied_onto_placements_by_default(self, project, mcp, lib_dir):
        _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor", dry_run=False)
        props = _call(mcp, "get_symbol_properties", reference="R1")["properties"]
        assert props["Value"] == "10k", "the placement's Value must survive"

    def test_named_fields_are_copied(self, project, mcp, lib_dir):
        _place_r1(mcp)
        # The placement copied the old Description at add time, so change the
        # library's before refreshing or there is nothing to write.
        lib = lib_dir / "MiniLib.kicad_sym"
        lib.write_text(
            lib.read_text(encoding="utf-8").replace(
                "Generic resistor", "Generic resistor, revised", 1
            ),
            encoding="utf-8",
        )
        project["reindex"]()

        res = _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor",
                    update_fields=["Description"], dry_run=False)
        written = res["reports"][0]["fields_written"]
        assert [w["field"] for w in written] == ["Description"]
        assert written[0]["from"] == "Generic resistor"
        assert written[0]["to"] == "Generic resistor, revised"
        props = _call(mcp, "get_symbol_properties", reference="R1")["properties"]
        assert props["Description"] == "Generic resistor, revised"

    def test_overwriting_value_is_possible_but_must_be_asked_for(
        self, project, mcp, lib_dir
    ):
        _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor",
              update_fields=["Value"], dry_run=False)
        props = _call(mcp, "get_symbol_properties", reference="R1")["properties"]
        assert props["Value"] == "Resistor"

    def test_a_field_already_matching_is_not_reported_as_written(
        self, project, mcp, lib_dir
    ):
        _place_r1(mcp)
        _move_pin1(lib_dir)
        project["reindex"]()
        _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor",
              update_fields=["Description"], dry_run=False)
        res = _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor",
                    update_fields=["Description"], dry_run=False)
        assert res["reports"][0]["fields_written"] == []


# ===== Adapter level ======================================================= #


class TestAdapter:
    def test_replace_lib_symbol_reports_a_miss(self, project, mcp):
        _place_r1(mcp)
        tree = sch_io.parse_file(project["sch"])
        node = [sch_io.sym("symbol"), "MiniLib:NotThere"]
        assert ed.replace_lib_symbol(tree, node) is False

    def test_diff_on_an_uncached_id_raises(self, project, mcp, lib_dir):
        _place_r1(mcp)
        tree = sch_io.parse_file(project["sch"])
        fresh = ed.fetch_symbol_def(lib_dir / "MiniLib.kicad_sym", "ESP32_Demo")
        with pytest.raises(KeyError, match="not cached"):
            ed.diff_lib_symbol(tree, "MiniLib:ESP32_Demo", fresh)

    def test_diff_reports_added_and_removed_pins(self, project, mcp, lib_dir):
        _place_r1(mcp)
        tree = sch_io.parse_file(project["sch"])
        # ESP32_Demo has three pins where Resistor has two.
        fresh = ed.fetch_symbol_def(lib_dir / "MiniLib.kicad_sym", "ESP32_Demo")
        diff = ed.diff_lib_symbol(tree, "MiniLib:Resistor", fresh)
        assert diff["pins_added"] == ["3"]
        assert diff["changed"] is True

    def test_instance_owned_fields_are_named_for_callers(self):
        assert "Reference" in ed.INSTANCE_OWNED_FIELDS
        assert "Value" in ed.INSTANCE_OWNED_FIELDS
        assert "Footprint" in ed.INSTANCE_OWNED_FIELDS


@pytest.mark.slow
def test_refreshed_sheet_still_loads_in_kicad_cli(project, mcp, lib_dir):
    """A refresh rewrites the lib_symbols block — the sheet must still load."""
    import subprocess

    from kicad_claude.utils.kicad_paths import find_kicad_cli

    cli = find_kicad_cli()
    if not cli:
        pytest.skip("no kicad-cli available")

    _place_r1(mcp, wired=True)
    _move_pin1(lib_dir)
    project["reindex"]()
    _call(mcp, "refresh_lib_symbols", lib_id="MiniLib:Resistor", dry_run=False)

    sch_path = Path(project["sch"])
    r = subprocess.run(
        [str(cli), "sch", "erc", str(sch_path)],
        capture_output=True, text=True, timeout=60, cwd=sch_path.parent,
    )
    # ERC flags the wire the refresh orphaned; the file itself must still load.
    assert "Failed to load" not in (r.stdout + r.stderr)
    assert r.returncode in (0, 5), f"unexpected exit {r.returncode}: {r.stderr}"
