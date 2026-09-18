"""Coordinate-system helpers.

Schematic tools use **KiCAD-native coordinates**: millimetres, Y axis pointing
DOWN, origin at the page's top-left — the same numbers the KiCAD GUI shows.
`sch_to_file_xy` is therefore the identity, and exists so the boundary stays
explicit. Schematic placements snap to `SCHEMATIC_GRID_MM` by default
(`snap_xy`), because KiCAD only connects items whose endpoints share a grid
point.

PCB tools use KiCAD-native coordinates too, so `pcb_to_file_xy` is likewise the
identity. They used to flip Y around the page height, which made every
coordinate page-size dependent and knocked grid points off the grid (297 mm is
not a multiple of 1.27 mm) — issue 2 in `docs/kicad_mcp_issues.md`.
"""

from __future__ import annotations

import math

# A4 landscape — KiCAD's default schematic page (x_max=297, y_max=210 mm).
DEFAULT_PAGE_HEIGHT_MM = 210.0

# KiCAD's default schematic grid: 50 mil.
SCHEMATIC_GRID_MM = 1.27


def sch_to_file_xy(x_mm: float, y_mm: float) -> tuple[float, float]:
    """Schematic MCP coords → file coords. Identity: both are KiCAD-native."""
    return float(x_mm), float(y_mm)


def file_to_sch_xy(x_mm: float, y_mm: float) -> tuple[float, float]:
    """File coords → schematic MCP coords. Identity; inverse of `sch_to_file_xy`."""
    return float(x_mm), float(y_mm)


def snap_mm(value: float, grid_mm: float = SCHEMATIC_GRID_MM) -> float:
    """Round `value` to the nearest multiple of `grid_mm`."""
    if grid_mm <= 0:
        raise ValueError(f"grid_mm must be > 0 (got {grid_mm})")
    return round_mm(round(float(value) / grid_mm) * grid_mm)


def snap_xy(
    x_mm: float, y_mm: float, grid_mm: float = SCHEMATIC_GRID_MM
) -> tuple[float, float]:
    """Snap both coordinates to `grid_mm`."""
    return snap_mm(x_mm, grid_mm), snap_mm(y_mm, grid_mm)


def pcb_to_file_xy(x_mm: float, y_mm: float) -> tuple[float, float]:
    """PCB MCP coords → file coords. Identity: both are KiCAD-native (Y down).

    The numbers are the ones the KiCAD PCB editor shows, and a grid point maps
    to a grid point.
    """
    return float(x_mm), float(y_mm)


def file_to_pcb_xy(x_mm: float, y_mm: float) -> tuple[float, float]:
    """File coords → PCB MCP coords. Identity; inverse of `pcb_to_file_xy`."""
    return float(x_mm), float(y_mm)


def normalize_rotation(deg: float) -> int:
    """Snap a rotation to {0, 90, 180, 270}. Phase 3 only supports right angles."""
    r = int(round(float(deg))) % 360
    if r not in (0, 90, 180, 270):
        raise ValueError(
            f"rotation must be a multiple of 90° (got {deg}); free angles unsupported"
        )
    return r


def rotate_xy(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate a vector around the origin by `deg` (counter-clockwise, math sense).

    KiCAD's rotation in the schematic file is also CCW around the symbol origin.
    """
    rad = math.radians(deg)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)
    return x * cos_r - y * sin_r, x * sin_r + y * cos_r


def round_mm(value: float, digits: int = 4) -> float:
    """KiCAD uses 6-decimal precision internally; 4 is plenty for placements."""
    return round(float(value), digits)
