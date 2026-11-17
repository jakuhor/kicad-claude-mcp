"""Active KiCAD project — module-level singleton.

Tools that operate on the project (add_symbol, autoroute_pcb, …) read from
this. `set_project` writes to it. The MCP server has a single active
project at a time.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from kicad_claude.adapters import sch_io


@dataclass(frozen=True)
class ActiveProject:
    """Paths and identity of the currently active KiCAD project."""

    path: Path
    name: str

    @property
    def pro_path(self) -> Path:
        return self.path / f"{self.name}.kicad_pro"

    @property
    def sch_path(self) -> Path:
        return self.path / f"{self.name}.kicad_sch"

    @property
    def pcb_path(self) -> Path:
        return self.path / f"{self.name}.kicad_pcb"

    def validate(self) -> None:
        """Raise FileNotFoundError if any of the three project files is missing."""
        for p in (self.pro_path, self.sch_path, self.pcb_path):
            if not p.is_file():
                raise FileNotFoundError(f"missing KiCAD project file: {p}")


_active: ActiveProject | None = None
_active_sheet: str | None = None  # filename relative to proj.path; None == root
_active_board: str | None = None  # filename relative to proj.path; None == proj.pcb_path


class NoActiveProjectError(RuntimeError):
    """Raised when a tool needs a project but none has been set."""


def set_active(path: Path | str, name: str) -> ActiveProject:
    """Mark a project as active. Validates that the three files exist."""
    global _active, _active_sheet, _active_board
    project = ActiveProject(path=Path(path).resolve(), name=name)
    project.validate()
    _active = project
    _active_sheet = None  # reset to root on project switch
    _active_board = None  # reset to default board on project switch
    return project


def get_active() -> ActiveProject:
    """Return the active project. Raises if none is set."""
    if _active is None:
        raise NoActiveProjectError(
            "No active KiCAD project. Call set_project or create_project first."
        )
    return _active


def get_active_or_none() -> ActiveProject | None:
    return _active


def clear_active() -> None:
    """Reset the active project (used in tests)."""
    global _active, _active_sheet, _active_board
    _active = None
    _active_sheet = None
    _active_board = None


# --------------------------------------------------------------------------- #
# Active sheet (hierarchical schematic context)
# --------------------------------------------------------------------------- #


def set_active_sheet(filename: str | None) -> None:
    """Switch the active sub-sheet. `None` or empty string returns to the root.

    Filename is relative to the project directory (e.g. "power_supply.kicad_sch").
    """
    global _active_sheet
    if not filename:
        _active_sheet = None
        return
    if not filename.endswith(".kicad_sch"):
        raise ValueError(f"sheet filename must end with .kicad_sch (got {filename!r})")
    proj = get_active()
    if not (proj.path / filename).is_file():
        raise FileNotFoundError(f"{proj.path / filename} does not exist")
    # The root schematic by filename is still the root: store None so every
    # tool takes its root path (instance path `/{root_uuid}`, etc.) instead of
    # searching the root for a child sheet with its own name.
    if (proj.path / filename).resolve() == Path(proj.sch_path).resolve():
        _active_sheet = None
        return
    _active_sheet = filename


def get_active_sheet_filename() -> str | None:
    """Return the active sheet's filename, or None when on the root sheet."""
    return _active_sheet


def get_active_sheet_path() -> Path:
    """Return the absolute path of the active schematic file (root or child)."""
    proj = get_active()
    if _active_sheet is None:
        return proj.sch_path
    return proj.path / _active_sheet


# --------------------------------------------------------------------------- #
# Active PCB / multi-board projects
# --------------------------------------------------------------------------- #


def set_active_board(filename: str | None) -> None:
    """Switch the active PCB. `None` or empty returns to the default `proj.pcb_path`."""
    global _active_board
    if not filename:
        _active_board = None
        return
    if not filename.endswith(".kicad_pcb"):
        raise ValueError(f"board filename must end with .kicad_pcb (got {filename!r})")
    proj = get_active()
    if not (proj.path / filename).is_file():
        raise FileNotFoundError(f"{proj.path / filename} does not exist")
    _active_board = filename


def get_active_board_filename() -> str | None:
    """Return the active board filename (None when on the project's main PCB)."""
    return _active_board


def get_active_board_path() -> Path:
    """Return the absolute path of the active .kicad_pcb."""
    proj = get_active()
    if _active_board is None:
        return proj.pcb_path
    return proj.path / _active_board


# --------------------------------------------------------------------------- #
# One call on another sheet / board, without changing the active one
# --------------------------------------------------------------------------- #


@contextmanager
def using_sheet(filename: str | None):
    """Make `filename` (None: root) the active sheet for the `with` block."""
    global _active_sheet
    previous = _active_sheet
    set_active_sheet(filename)
    try:
        yield
    finally:
        _active_sheet = previous


@contextmanager
def using_board(filename: str | None):
    """Make `filename` (None: the project's board) active for the `with` block."""
    global _active_board
    previous = _active_board
    set_active_board(filename)
    try:
        yield
    finally:
        _active_board = previous


# --------------------------------------------------------------------------- #
# Session memory: the active project survives a server restart (OP-34)
# --------------------------------------------------------------------------- #

_session_file: Path | None = None   # set by server.py only; tests never write


def enable_session(path: Path) -> None:
    """Remember the active project in `path`, keyed by the working directory."""
    global _session_file
    _session_file = Path(path)


def _session_key() -> str:
    return str(Path(os.getcwd()).resolve())


def remember() -> None:
    """Store the active project / sheet / board for this working directory.
    Called by the tools that change them on purpose, not by `using_*`."""
    if _session_file is None:
        return
    try:
        data = json.loads(_session_file.read_text(encoding="utf-8")) if _session_file.is_file() else {}
    except (OSError, ValueError):
        data = {}
    if _active is None:
        data.pop(_session_key(), None)
    else:
        data[_session_key()] = {"path": str(_active.path), "name": _active.name,
                                "sheet": _active_sheet, "board": _active_board}
    try:
        _session_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: another server process reading mid-write would see broken
        # JSON, fall back to {} and drop every other directory's entry.
        sch_io.atomic_write_bytes(_session_file, json.dumps(data, indent=1).encode("utf-8"))
    except OSError as e:
        logging.getLogger("kicad-claude.state").warning("session not saved: %s", e)


def restore() -> ActiveProject | None:
    """Reactivate what `remember` stored for this working directory, when its
    files still exist. Returns the project, or None."""
    global _active_sheet, _active_board
    if _session_file is None or not _session_file.is_file():
        return None
    try:
        rec = json.loads(_session_file.read_text(encoding="utf-8")).get(_session_key())
        if not rec:
            return None
        proj = set_active(rec["path"], rec["name"])
    except (OSError, ValueError, KeyError, FileNotFoundError) as e:
        logging.getLogger("kicad-claude.state").info("session not restored: %s", e)
        return None
    for setter, value in ((set_active_sheet, rec.get("sheet")), (set_active_board, rec.get("board"))):
        try:
            setter(value)
        except (OSError, ValueError):
            pass
    return proj
