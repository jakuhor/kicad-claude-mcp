"""Autorouting via Freerouting.

Strategy:
- Unit tests for path detection and stats parsing (no network, no Java).
- @pytest.mark.slow: full pipeline (export DSN → Freerouting → import SES)
  on a small board built with KiCAD official footprints. Skipped if any
  prerequisite (KiCAD bundled Python, Freerouting JAR, library index) is
  missing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import freerouting, kicad_python
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools.library import index as lib_tools
from kicad_claude.tools.pcb import layout as pcb_tools
from kicad_claude.tools.pcb import routing as routing_tools


# ===== Path detection ====================================================== #


def test_find_kicad_python_returns_existing_path():
    p = kicad_python.find_kicad_python()
    if p is None:
        pytest.skip("KiCAD bundled Python not present on this system")
    assert p.is_file()


def test_find_freerouting_jar_returns_existing_path():
    p = freerouting.find_freerouting_jar()
    if p is None:
        pytest.skip("freerouting.jar not present (not yet downloaded?)")
    assert p.is_file()
    # Sanity: jar files are at least a few MB.
    assert p.stat().st_size > 1_000_000


def test_find_java_present_in_environment():
    j = freerouting.find_java()
    if j is None:
        pytest.skip("`java` not on PATH")
    assert j.is_file()


# ===== Stats parser ======================================================== #


# Freerouting 2.5.0 output (KiCAD demo ecc83-pp, 5 passes), board hashes shortened.
SAMPLE_LOG = """
2026-11-12 22:56:22.101 INFO   Freerouting v2.5.0 (build-date: 2026-10-05)
2026-11-12 22:56:22.912 INFO   [9290DC\\FFB21B] Auto-routing stage started on board '0cef' with baseline score 365.08 for 10 unrouted items.
2026-11-12 22:56:23.330 INFO   [9290DC\\FFB21B] Auto-routing pass #1 on board '0cef' was completed in 0.41 seconds with score 600.00 (8 unrouted and 1 violations).
2026-11-12 22:56:23.960 INFO   [9290DC\\FFB21B] Auto-routing pass #5 on board '577e' was completed in 0.12 seconds with score 619.05 (6 unrouted and 0 violations), using 0.98 CPU seconds and the job allocated 0.13 GB of memory so far.
2026-11-12 22:56:23.975 INFO   [9290DC\\FFB21B] Auto-routing stage completed: started with 10 unrouted nets, completed in 1.19 seconds, final score: 619.05 (6 unrouted and 0 violations), using 0.98 total CPU seconds, 0.13 GB total allocated, and 47.1 MB peak heap usage.
2026-11-12 22:56:24.471 INFO   [9290DC\\FFB21B] Skipping optimization stage: the board has 6 unrouted connection(s) (optimizer only runs on completely routed boards).
2026-11-12 22:56:24.487 INFO   [9290DC\\FFB21B] Optimization was completed in 0.02 seconds with the score of 619.05 (6 unrouted and 0 violations).
"""


def test_parse_stats_extracts_known_fields():
    stats = freerouting._parse_stats(SAMPLE_LOG)
    # The last pass and the last score line give the final state.
    assert stats == {
        "passes_done": 5,
        "score": 619.05,
        "unrouted": 6,
        "violations": 0,
        "unrouted_start": 10,
        "duration_s": 1.19,
    }


def test_route_passes_max_passes_with_the_2_5_flag(tmp_path, monkeypatch):
    """2.5.0 deprecates `-mp`; the pass count goes in `router.autorouter.max_passes`."""
    dsn = tmp_path / "b.dsn"
    dsn.write_text("(pcb)", encoding="utf-8")
    ses = tmp_path / "b.ses"
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        ses.write_text("(session)", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=SAMPLE_LOG, stderr="")

    monkeypatch.setattr(freerouting, "_ensure_environment",
                        lambda: (Path("fr.jar"), Path("java")))
    monkeypatch.setattr(freerouting.subprocess, "run", fake_run)
    res = freerouting.route(dsn, ses, passes=7)
    assert "--router.autorouter.max_passes=7" in seen["cmd"]
    assert "-mp" not in seen["cmd"]
    assert res["stats"]["unrouted"] == 6


def test_parse_stats_empty_when_no_matches():
    assert freerouting._parse_stats("nothing relevant here") == {}


# ===== Full pipeline (slow) ================================================ #


def _have_prerequisites() -> tuple[bool, str]:
    if kicad_python.find_kicad_python() is None:
        return False, "KiCAD bundled Python not found"
    if freerouting.find_freerouting_jar() is None:
        return False, "freerouting.jar not found"
    if freerouting.find_java() is None:
        return False, "java not on PATH"
    if kicad_libs.load_cache() is None:
        return False, "library index not built"
    return True, ""


@pytest.mark.slow
def test_export_dsn_then_import_ses_pipeline(tmp_path: Path):
    """Build a tiny board, export DSN, run Freerouting, import SES — file grows."""
    ok, why = _have_prerequisites()
    if not ok:
        pytest.skip(why)

    state.clear_active()
    lib_tools._index = kicad_libs.load_cache()

    files = write_blank_project(tmp_path / "rt", "rt")
    state.set_active(tmp_path / "rt", "rt")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    pcb_tools.register(mcp)
    routing_tools.register(mcp)

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    call("set_board_outline", width_mm=50, height_mm=30)
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R1", value="10k", x_mm=20, y_mm=15)
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R2", value="1k", x_mm=35, y_mm=15)

    pcb_path = files["pcb"]
    size_before_export = pcb_path.stat().st_size

    # 1) Export DSN
    res = call("export_dsn")
    dsn = Path(res["dsn_path"])
    assert dsn.is_file()
    assert dsn.stat().st_size > 100

    # 2) Run Freerouting (low passes — no nets means it terminates fast anyway)
    route_res = freerouting.route(
        dsn, dsn.with_suffix(".ses"), passes=5, timeout_seconds=60,
    )
    assert route_res["returncode"] == 0
    ses = Path(route_res["ses_path"])
    assert ses.is_file()

    # 3) Import SES — should not error and the PCB should still be parseable.
    import_res = call("import_ses", ses_path=str(ses))
    assert Path(import_res["pcb_path"]).is_file()
    assert pcb_path.stat().st_size >= size_before_export
    state.clear_active()


@pytest.mark.slow
def test_autoroute_pcb_tool_end_to_end(tmp_path: Path):
    """Full single-tool autoroute flow."""
    ok, why = _have_prerequisites()
    if not ok:
        pytest.skip(why)

    state.clear_active()
    lib_tools._index = kicad_libs.load_cache()

    write_blank_project(tmp_path / "ar", "ar")
    state.set_active(tmp_path / "ar", "ar")

    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t")
    pcb_tools.register(mcp)
    routing_tools.register(mcp)

    def call(name, **kw):
        return mcp._tool_manager.get_tool(name).fn(**kw)

    call("set_board_outline", width_mm=50, height_mm=30)
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R1", value="10k", x_mm=20, y_mm=15)
    call("add_footprint", lib_id="Resistor_SMD:R_0603_1608Metric",
         reference="R2", value="1k", x_mm=35, y_mm=15)

    res = call("autoroute_pcb", passes=5, timeout_seconds=60)
    assert res["freerouting_returncode"] == 0
    assert Path(res["dsn_path"]).is_file()
    assert Path(res["ses_path"]).is_file()
    assert Path(res["pcb_path"]).is_file()
    state.clear_active()


def test_versioned_jar_names_are_found(tmp_path, monkeypatch):
    """Releases download as `freerouting-2.1.0.jar`, not `freerouting.jar`."""
    from pathlib import Path

    third_party = tmp_path / "third_party"
    third_party.mkdir()
    (third_party / "freerouting-2.0.1.jar").write_bytes(b"old")
    (third_party / "freerouting-2.1.0.jar").write_bytes(b"new")
    monkeypatch.delenv("FREEROUTING_JAR", raising=False)
    monkeypatch.setattr(
        freerouting, "__file__",
        str(tmp_path / "src" / "kicad_claude" / "adapters" / "freerouting.py"),
    )
    found = freerouting.find_freerouting_jar()
    assert found == Path(third_party / "freerouting-2.1.0.jar")


def test_unversioned_jar_still_wins(tmp_path, monkeypatch):
    from pathlib import Path

    third_party = tmp_path / "third_party"
    third_party.mkdir()
    (third_party / "freerouting.jar").write_bytes(b"plain")
    (third_party / "freerouting-2.1.0.jar").write_bytes(b"versioned")
    monkeypatch.delenv("FREEROUTING_JAR", raising=False)
    monkeypatch.setattr(
        freerouting, "__file__",
        str(tmp_path / "src" / "kicad_claude" / "adapters" / "freerouting.py"),
    )
    assert freerouting.find_freerouting_jar() == Path(third_party / "freerouting.jar")
