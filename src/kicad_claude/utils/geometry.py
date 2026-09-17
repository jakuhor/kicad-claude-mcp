"""Coordinate-system helpers.

Schematic tools use **KiCAD-native coordinates**: millimetres, Y axis pointing
DOWN, origin at the page's top-left — the same numbers the KiCAD GUI shows.
`sch_to_file_xy` is therefore the identity, and exists so the boundary stays
explicit. Schematic placements snap to `SCHEMATIC_GRID_MM` by default
(`snap_xy`), because KiCAD only connects items whose endpoints share a grid
point.

PCB tools still use the old convention: Y UP, flipped around the page height by
`mcp_to_kicad_xy` / `kicad_to_mcp_xy`. Those two are page-size dependent and a
grid point does not survive the flip (297 mm is not a multiple of 1.27 mm), so
the PCB side is meant to move to native coordinates in a later pass.
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


def mcp_to_kicad_xy(
    x_mm: float, y_mm: float, page_height_mm: float = DEFAULT_PAGE_HEIGHT_MM
) -> tuple[float, float]:
    """Translate a point from PCB MCP coords (Y up) to KiCAD file coords (Y down).

    PCB only — schematic code uses `sch_to_file_xy`.
    """
    return float(x_mm), float(page_height_mm) - float(y_mm)


def kicad_to_mcp_xy(
    x_mm: float, y_mm: float, page_height_mm: float = DEFAULT_PAGE_HEIGHT_MM
) -> tuple[float, float]:
    """Inverse of mcp_to_kicad_xy. The transform is its own inverse. PCB only."""
    return float(x_mm), float(page_height_mm) - float(y_mm)


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
