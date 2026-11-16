"""The active project survives a server restart (OP-34)."""

from __future__ import annotations

import json

import pytest

from kicad_claude import state
from kicad_claude.templates.blank import write_blank_project


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state.clear_active()
    state.enable_session(tmp_path / "cache" / "session.json")
    yield tmp_path / "cache" / "session.json"
    state._session_file = None
    state.clear_active()


def test_restart_restores_project_and_board(session, tmp_path):
    write_blank_project(tmp_path / "p", "p")
    (tmp_path / "p" / "alt.kicad_pcb").write_text((tmp_path / "p" / "p.kicad_pcb").read_text())
    state.set_active(tmp_path / "p", "p")
    state.set_active_board("alt.kicad_pcb")
    state.remember()
    state.clear_active()                       # the restart
    proj = state.restore()
    assert proj is not None and proj.name == "p"
    assert state.get_active_board_filename() == "alt.kicad_pcb"


def test_no_restore_when_the_project_is_gone_or_another_dir(session, tmp_path, monkeypatch):
    write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    state.remember()
    state.clear_active()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert state.restore() is None             # keyed by working directory
    monkeypatch.chdir(tmp_path)
    (tmp_path / "p" / "p.kicad_sch").unlink()
    assert state.restore() is None             # files gone
    assert len(json.loads(session.read_text())) == 1


def test_nothing_written_without_enable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert state._session_file is None
    state.remember()                           # tests and library use: no file
    assert not any(tmp_path.iterdir())
