"""Phase 18 — the guarded write path (P3, P4, P5).

Every mutating tool goes through `adapters/safe_write.save_tree`, which refuses
to write while KiCAD holds the project open, proves the written file still
parses, and keeps the backup directory bounded.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sexpdata

from kicad_claude import state
from kicad_claude.adapters import safe_write, sch_io
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import schematic as sch_tools

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "guard", "guard")
    state.set_active(tmp_path / "guard", "guard")
    yield files
    state.clear_active()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(safe_write.IGNORE_LOCK_ENV, raising=False)
    monkeypatch.delenv(safe_write.VERIFY_ENV, raising=False)


def _make_mcp(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP
    from tests.test_phase3_schematic import _patched_index_with_minilib

    idx = _patched_index_with_minilib(tmp_path)
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)
    mcp = FastMCP("test")
    sch_tools.register(mcp)
    lib_tools.register(mcp)
    return mcp


def _call(mcp, tool_name, /, **kwargs):
    return mcp._tool_manager.get_tool(tool_name).fn(**kwargs)


# ===== P5 — KiCAD lock file ================================================ #


class TestLockFile:
    def test_lock_names_cover_the_file_and_the_project(self, blank_project):
        sch = Path(blank_project["sch"])
        names = {p.name for p in safe_write.lock_files_for(sch)}
        assert "~guard.kicad_sch.lck" in names
        assert "~guard.kicad_pro.lck" in names

    def test_no_lock_means_no_complaint(self, blank_project):
        safe_write.check_not_locked(Path(blank_project["sch"]))

    def test_a_sheet_lock_blocks_the_write(self, blank_project):
        sch = Path(blank_project["sch"])
        (sch.parent / f"~{sch.name}.lck").write_text("", encoding="utf-8")
        with pytest.raises(safe_write.ProjectLockedError, match="KiCAD has this project open"):
            safe_write.check_not_locked(sch)

    def test_a_project_lock_blocks_the_write(self, blank_project):
        sch = Path(blank_project["sch"])
        (sch.parent / "~guard.kicad_pro.lck").write_text("", encoding="utf-8")
        with pytest.raises(safe_write.ProjectLockedError):
            safe_write.check_not_locked(sch)

    def test_the_error_names_the_lock_and_the_override(self, blank_project):
        sch = Path(blank_project["sch"])
        (sch.parent / "~guard.kicad_pro.lck").write_text("", encoding="utf-8")
        with pytest.raises(safe_write.ProjectLockedError) as exc:
            safe_write.check_not_locked(sch)
        assert "~guard.kicad_pro.lck" in str(exc.value)
        assert safe_write.IGNORE_LOCK_ENV in str(exc.value)

    def test_the_override_lets_a_stale_lock_through(self, blank_project, monkeypatch):
        sch = Path(blank_project["sch"])
        (sch.parent / "~guard.kicad_pro.lck").write_text("", encoding="utf-8")
        monkeypatch.setenv(safe_write.IGNORE_LOCK_ENV, "1")
        safe_write.check_not_locked(sch)

    def test_a_locked_project_stops_a_tool(self, blank_project, tmp_path, monkeypatch):
        mcp = _make_mcp(monkeypatch, tmp_path)
        sch = Path(blank_project["sch"])
        before = sch.read_bytes()
        (sch.parent / "~guard.kicad_pro.lck").write_text("", encoding="utf-8")

        with pytest.raises(safe_write.ProjectLockedError):
            _call(mcp, "add_wire", x1_mm=50.8, y1_mm=50.8, x2_mm=76.2, y2_mm=50.8)
        assert sch.read_bytes() == before, "the sheet must be untouched"


# ===== P3 — the write is proved to parse =================================== #


class TestWriteVerification:
    def test_a_good_write_returns_its_backup(self, blank_project):
        sch = Path(blank_project["sch"])
        tree = sch_io.parse_file(sch)
        backup = safe_write.save_tree(sch, tree)
        assert backup is not None and backup.is_file()
        assert sch_io.parse_file(sch)

    def test_a_first_write_with_no_previous_file_has_no_backup(self, tmp_path):
        target = tmp_path / "fresh.kicad_sch"
        tree = [sexpdata.Symbol("kicad_sch"), [sexpdata.Symbol("version"), 20260306]]
        assert safe_write.save_tree(target, tree) is None
        assert target.is_file()

    def test_an_unparseable_write_is_rolled_back(self, blank_project, monkeypatch):
        """The guard that issue 1 needed: a corrupt file never survives."""
        sch = Path(blank_project["sch"])
        good = sch.read_bytes()

        def _corrupt(path, tree):
            Path(path).write_text("(kicad_sch (version", encoding="utf-8")

        monkeypatch.setattr(safe_write.sch_io, "write_file", _corrupt)
        with pytest.raises(RuntimeError, match="does not parse"):
            safe_write.save_tree(sch, sch_io.parse_file(sch))

        assert sch.read_bytes() == good, "the good version must be back"

    def test_the_rollback_error_names_the_backup(self, blank_project, monkeypatch):
        sch = Path(blank_project["sch"])

        def _corrupt(path, tree):
            Path(path).write_text("(((", encoding="utf-8")

        monkeypatch.setattr(safe_write.sch_io, "write_file", _corrupt)
        with pytest.raises(RuntimeError) as exc:
            safe_write.save_tree(sch, sch_io.parse_file(sch))
        assert ".backups" in str(exc.value)

    def test_verification_can_be_switched_off(self, blank_project, monkeypatch):
        sch = Path(blank_project["sch"])

        def _corrupt(path, tree):
            Path(path).write_text("(((", encoding="utf-8")

        monkeypatch.setattr(safe_write.sch_io, "write_file", _corrupt)
        monkeypatch.setenv(safe_write.VERIFY_ENV, "0")
        safe_write.save_tree(sch, sch_io.parse_file(sch))
        assert sch.read_text(encoding="utf-8") == "((("

    def test_explicit_verify_beats_the_environment(self, blank_project, monkeypatch):
        sch = Path(blank_project["sch"])

        def _corrupt(path, tree):
            Path(path).write_text("(((", encoding="utf-8")

        monkeypatch.setattr(safe_write.sch_io, "write_file", _corrupt)
        monkeypatch.setenv(safe_write.VERIFY_ENV, "0")
        with pytest.raises(RuntimeError, match="does not parse"):
            safe_write.save_tree(sch, sch_io.parse_file(sch), verify=True)


# ===== P4 — bounded backups ================================================ #


class TestBackupRetention:
    def test_backups_stop_piling_up(self, blank_project):
        sch = Path(blank_project["sch"])
        tree = sch_io.parse_file(sch)
        for _ in range(safe_write.DEFAULT_KEEP_BACKUPS + 15):
            safe_write.save_tree(sch, tree)

        kept = list((sch.parent / ".backups").glob(f"*_{sch.name}"))
        assert len(kept) == safe_write.DEFAULT_KEEP_BACKUPS

    def test_the_newest_backups_are_the_ones_kept(self, blank_project):
        sch = Path(blank_project["sch"])
        backups = sch.parent / ".backups"
        backups.mkdir(exist_ok=True)
        for i in range(20):
            (backups / f"2026010{i // 10}-00000{i % 10}_{sch.name}").write_text(
                str(i), encoding="utf-8"
            )
        safe_write.prune_backups(backups, sch.name, keep=5)

        left = sorted(p.name for p in backups.glob(f"*_{sch.name}"))
        assert len(left) == 5
        assert left == sorted(left, reverse=False)
        assert left[0] > "20260101-000004"

    def test_other_files_are_not_pruned(self, blank_project):
        sch = Path(blank_project["sch"])
        backups = sch.parent / ".backups"
        backups.mkdir(exist_ok=True)
        for i in range(8):
            (backups / f"2026010{i}-000000_{sch.name}").write_text("x", encoding="utf-8")
            (backups / f"2026010{i}-000000_other.kicad_pcb").write_text("x", encoding="utf-8")

        safe_write.prune_backups(backups, sch.name, keep=2)
        assert len(list(backups.glob("*_other.kicad_pcb"))) == 8

    def test_keep_zero_prunes_nothing(self, blank_project):
        sch = Path(blank_project["sch"])
        backups = sch.parent / ".backups"
        backups.mkdir(exist_ok=True)
        (backups / f"20260101-000000_{sch.name}").write_text("x", encoding="utf-8")
        assert safe_write.prune_backups(backups, sch.name, keep=0) == 0

    def test_a_long_tool_session_leaves_a_bounded_directory(
        self, blank_project, tmp_path, monkeypatch
    ):
        """Issue 9's symptom: 88 backups from one session."""
        mcp = _make_mcp(monkeypatch, tmp_path)
        for i in range(25):
            _call(mcp, "add_wire", x1_mm=50.8, y1_mm=50.8 + i * 1.27,
                  x2_mm=76.2, y2_mm=50.8 + i * 1.27)

        sch = Path(blank_project["sch"])
        kept = list((sch.parent / ".backups").glob(f"*_{sch.name}"))
        assert len(kept) == safe_write.DEFAULT_KEEP_BACKUPS
