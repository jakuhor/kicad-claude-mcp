"""Phase 1 — KiCAD project management tools.

Tools:
    create_project       — generate a blank KiCAD project + mark active
    set_project          — mark an existing project as active
    get_project_state    — summary of active project (counts, paths)
    list_components      — list symbols in the active schematic
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.utils.kicad_strings import normalize_name

logger = logging.getLogger("kicad-claude.tools.project")


def _resolve_project_dir_and_name(project_path: str) -> tuple[Path, str]:
    """Accept either a project directory or any of its .kicad_* files.

    Returns (directory, project_name). Raises FileNotFoundError or ValueError
    if the path is ambiguous or no project file is found.
    """
    p = Path(project_path).expanduser().resolve()
    if p.is_file():
        if p.suffix not in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
            raise ValueError(f"unsupported file extension: {p.suffix}")
        return p.parent, p.stem

    if not p.is_dir():
        raise FileNotFoundError(f"path not found: {p}")

    pros = sorted(p.glob("*.kicad_pro"))
    if not pros:
        raise FileNotFoundError(f"no .kicad_pro file in {p}")
    if len(pros) > 1:
        names = [x.name for x in pros]
        raise ValueError(
            f"multiple .kicad_pro files in {p}: {names}; pass the file path directly"
        )
    return p, pros[0].stem


def _summarize(proj: state.ActiveProject) -> dict:
    """Cheap counts of an active project: symbols, footprints, nets.

    Read with this project's own parsers. `kicad-skip` was used here until it
    turned out it cannot parse a KiCAD 10 footprint's text items, which made
    `set_project` raise on every board that had a footprint on it.
    """
    sch_tree = sch_io.parse_file(proj.sch_path)
    pcb_tree = sch_io.parse_file(proj.pcb_path)
    return {
        "path": str(proj.path),
        "name": proj.name,
        "files": {
            "pro": str(proj.pro_path),
            "sch": str(proj.sch_path),
            "pcb": str(proj.pcb_path),
        },
        "symbols": sum(1 for _ in ed.iter_instance_symbols(sch_tree)),
        "footprints": sum(1 for _ in pcb_ed.iter_footprints(pcb_tree)),
        "nets": len(pcb_ed.list_nets(pcb_tree)),
    }


def _component_dict(sym: list) -> dict:
    """Best-effort extraction of a symbol's identity. Tolerant of missing fields."""
    reference = ed.get_symbol_property(sym, "Reference") or "?"
    value = ed.get_symbol_property(sym, "Value") or ""
    lib_id_node = sch_io.find_child(sym, "lib_id")
    lib_id = str(lib_id_node[1]) if lib_id_node and len(lib_id_node) > 1 else ""
    at = sch_io.find_child(sym, "at") or []
    x = float(at[1]) if len(at) > 1 else 0.0
    y = float(at[2]) if len(at) > 2 else 0.0
    rotation = float(at[3]) if len(at) > 3 else 0.0
    return {
        # Shown to the caller, so decode KiCAD's `{brace}` escapes.
        "reference": normalize_name(reference) if isinstance(reference, str) else reference,
        "value": normalize_name(value) if isinstance(value, str) else value,
        "lib_id": lib_id,
        "position_mm": [x, y],
        "rotation": rotation,
    }


def register(mcp) -> None:
    """Register Phase 1 tools on the FastMCP instance."""

    @mcp.tool()
    def create_project(path: str, name: str) -> dict:
        """Create a new blank KiCAD project at `path` named `name`.

        Writes `{name}.kicad_pro`, `{name}.kicad_sch`, `{name}.kicad_pcb`
        and marks the project as active. Refuses to overwrite if any of
        the three files already exists at the target.
        """
        target = Path(path).expanduser().resolve()
        files = write_blank_project(target, name)
        proj = state.set_active(target, name)
        logger.info("created project %s at %s", name, target)
        return {
            "path": str(target),
            "name": name,
            "files": {k: str(v) for k, v in files.items()},
            "active": True,
            **_summarize(proj),
        }

    @mcp.tool()
    def set_project(project_path: str) -> dict:
        """Mark an existing KiCAD project as active.

        `project_path` may be the project directory, or the path to any of
        its `.kicad_pro` / `.kicad_sch` / `.kicad_pcb` files.
        """
        directory, name = _resolve_project_dir_and_name(project_path)
        proj = state.set_active(directory, name)
        logger.info("active project: %s at %s", name, directory)
        return {"active": True, **_summarize(proj)}

    @mcp.tool()
    def get_project_state() -> dict:
        """Return a summary of the currently active KiCAD project.

        Includes paths and counts of symbols, footprints, nets.
        """
        return _summarize(state.get_active())

    @mcp.tool()
    def list_components(scope: str = "active") -> list[dict]:
        """List schematic symbols of the active project.

        `scope="active"` (default) lists the active sheet — the one set by
        `set_active_sheet`, root when unset. `scope="all"` walks the whole
        hierarchy.

        Each entry: reference, value, lib_id, position_mm [x, y], rotation,
        sheet (filename relative to the project directory).
        """
        if scope not in ("active", "all"):
            raise ValueError(f"scope must be 'active' or 'all' (got {scope!r})")
        proj = state.get_active()
        if scope == "active":
            paths = [state.get_active_sheet_path()]
        else:
            paths = ed.hierarchy_sch_paths(proj.sch_path)
        out: list[dict] = []
        for path in paths:
            tree = sch_io.parse_file(path)
            sheet_name = path.name
            for symbol in ed.iter_instance_symbols(tree):
                entry = _component_dict(symbol)
                entry["sheet"] = sheet_name
                out.append(entry)
        return out
