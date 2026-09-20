"""High-level mutations on a parsed `.kicad_pcb` tree.

Mirrors `sch_editor` but for PCBs. Same s-expression machinery (`sch_io`),
same coordinate convention: KiCAD-native millimetres, Y DOWN, the numbers the
PCB editor itself shows. `pcb_to_file_xy` is therefore the identity.

Until issue 2 was fixed, the API used Y up and flipped around the page height.
That made every coordinate depend on `(paper ...)` and knocked grid points off
the grid, because 297 mm (A3) is not a multiple of 1.27 mm.

Board origin: `set_board_outline` places the board with its TOP-left corner at
(`origin_x_mcp`, `origin_y_mcp`) — defaults (10, 10) — so the board occupies
(10..10+w, 10..10+h), extending right and down.
"""

from __future__ import annotations

import copy
import logging
import uuid
from pathlib import Path
from typing import Any

import sexpdata

import math
import re

from kicad_claude.adapters import length_tuning, pcb_layers, sch_io
from kicad_claude.adapters.sch_io import (
    find_child,
    find_children,
    get_property,
    head_of,
    is_call,
    property_name_index,
    sym,
)
from kicad_claude.utils.geometry import (
    file_to_pcb_xy,
    pcb_to_file_xy,
    normalize_rotation,
    round_mm,
)
from kicad_claude.utils.kicad_strings import normalize_name

logger = logging.getLogger("kicad-claude.adapters.pcb_editor")


# --------------------------------------------------------------------------- #
# Layer-count reconfiguration
# --------------------------------------------------------------------------- #


def get_copper_layer_count(tree: list) -> int:
    """Count the signal layers (copper) in the active PCB."""
    layers = find_child(tree, "layers")
    if not layers:
        return 0
    n = 0
    for row in layers[1:]:
        if isinstance(row, list) and len(row) >= 3:
            kind = row[2]
            if isinstance(kind, type(sym("x"))) and str(kind) == "signal":
                n += 1
    return n


def get_copper_layer_names(tree: list) -> list[str]:
    """Names of signal copper layers in file-order: F.Cu, In*.Cu..., B.Cu."""
    layers = find_child(tree, "layers")
    if not layers:
        return []
    names: list[str] = []
    for row in layers[1:]:
        if isinstance(row, list) and len(row) >= 3:
            kind = row[2]
            if isinstance(kind, type(sym("x"))) and str(kind) == "signal":
                if isinstance(row[1], str):
                    names.append(row[1])
    return names


def set_copper_layer_count(tree: list, n: int) -> dict:
    """Replace the `(layers ...)` block and the `(setup (stackup ...) ...)`
    sub-block to reflect `n` copper layers.

    Existing tracks/vias/footprints on layers that disappear are NOT migrated;
    they keep their layer name in the file but those layers won't exist, and
    KiCAD's DRC will complain. Set the layer count BEFORE adding tracks.
    """
    n = int(n)
    if n < 2 or n > pcb_layers.MAX_COPPER_LAYERS or n % 2:
        raise ValueError(
            f"copper layer count must be even and 2-{pcb_layers.MAX_COPPER_LAYERS} "
            f"(got {n})"
        )

    # Replace (layers ...)
    new_layers = pcb_layers.build_layers_block(n)
    for i, child in enumerate(tree):
        if is_call(child, "layers"):
            tree[i] = new_layers
            break
    else:
        # Insert near the top (after paper / general)
        insert_at = 1
        for i, child in enumerate(tree[1:], start=1):
            h = head_of(child)
            if h in ("paper", "general"):
                insert_at = i + 1
        tree.insert(insert_at, new_layers)

    # Replace (stackup ...) inside (setup ...)
    setup = find_child(tree, "setup")
    new_stackup = pcb_layers.build_stackup_block(n)
    if setup is None:
        # Setup block is required; create a minimal one with just the stackup.
        setup = [sym("setup"), new_stackup]
        tree.append(setup)
    else:
        replaced = False
        for i, child in enumerate(setup):
            if is_call(child, "stackup"):
                setup[i] = new_stackup
                replaced = True
                break
        if not replaced:
            setup.insert(1, new_stackup)

    return {
        "copper_layers": n,
        "layer_names": pcb_layers.copper_layer_names(n),
    }


# --------------------------------------------------------------------------- #
# Footprint lookup
# --------------------------------------------------------------------------- #


def iter_footprints(tree: list):
    for child in tree[1:]:
        if is_call(child, "footprint"):
            yield child


def _footprint_property(fp: list, name: str) -> str | None:
    return get_property(fp, name)


def get_fp_text(fp: list, kind: str) -> str | None:
    """Value of a legacy `(fp_text reference|value "X" …)` child, if present.

    KiCAD 7 replaced these with `(property "Reference" …)`, but a `.pretty`
    library written before that still uses them and KiCAD still loads it.
    """
    for node in find_children(fp, "fp_text"):
        if len(node) > 2 and str(node[1]) == kind and isinstance(node[2], str):
            return node[2]
    return None


def _set_fp_text(children: list, kind: str, value: str) -> bool:
    """Set every `(fp_text <kind> …)` in `children`. Returns True if any changed."""
    changed = False
    for node in children:
        if is_call(node, "fp_text") and len(node) > 2 and str(node[1]) == kind:
            node[2] = value
            changed = True
    return changed


def get_footprint_reference(fp: list) -> str | None:
    """The footprint's reference: the property, else the legacy `fp_text`."""
    ref = _footprint_property(fp, "Reference")
    if ref is not None:
        return ref
    return get_fp_text(fp, "reference")


def find_footprint_by_reference(tree: list, reference: str) -> list | None:
    for fp in iter_footprints(tree):
        if get_footprint_reference(fp) == reference:
            return fp
    return None


def all_footprint_references(tree: list) -> list[str]:
    return [get_footprint_reference(fp) or "?" for fp in iter_footprints(tree)]


# --------------------------------------------------------------------------- #
# Board outline (Edge.Cuts)
# --------------------------------------------------------------------------- #

EDGE_CUTS_GR_HEADS = {"gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly"}


def _is_edge_cuts_node(node: Any) -> bool:
    if not isinstance(node, list):
        return False
    if head_of(node) not in EDGE_CUTS_GR_HEADS:
        return False
    layer = find_child(node, "layer")
    return bool(layer and len(layer) >= 2 and layer[1] == "Edge.Cuts")


def remove_board_outline(tree: list) -> int:
    """Remove every Edge.Cuts graphic. Returns count removed."""
    removed = 0
    i = 1
    while i < len(tree):
        if _is_edge_cuts_node(tree[i]):
            tree.pop(i)
            removed += 1
        else:
            i += 1
    return removed


def set_board_outline(
    tree: list,
    width_mm: float,
    height_mm: float,
    shape: str = "rect",
    origin_x_mcp: float = 10.0,
    origin_y_mcp: float = 10.0,
) -> dict:
    """Replace the Edge.Cuts outline with a `width × height` rectangle.

    Coordinates are KiCAD-native (Y down), so the origin is the board's
    TOP-left corner and the rectangle extends right and down from it.
    Returns a summary including both diagonal corners.
    """
    if shape not in ("rect", "rounded_rect"):
        raise ValueError(f"shape must be 'rect' or 'rounded_rect' (got {shape!r})")
    if shape == "rounded_rect":
        # Rounded corners need 4 lines + 4 arcs; defer until Phase 6+.
        raise NotImplementedError("rounded_rect outline not yet implemented")

    remove_board_outline(tree)

    # Y is down, so the origin is the top-left corner.
    tl_mcp = (origin_x_mcp, origin_y_mcp)
    br_mcp = (origin_x_mcp + width_mm, origin_y_mcp + height_mm)

    tl_k = pcb_to_file_xy(*tl_mcp)
    br_k = pcb_to_file_xy(*br_mcp)

    # gr_rect's start/end are diagonal corners; KiCAD doesn't care about the order.
    node = [
        sym("gr_rect"),
        [sym("start"), round_mm(tl_k[0]), round_mm(tl_k[1])],
        [sym("end"), round_mm(br_k[0]), round_mm(br_k[1])],
        [sym("stroke"), [sym("width"), 0.15], [sym("type"), sym("solid")]],
        [sym("fill"), sym("no")],
        [sym("layer"), "Edge.Cuts"],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return {
        "shape": shape,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "top_left_mm": list(tl_mcp),
        "bottom_right_mm": list(br_mcp),
    }


# --------------------------------------------------------------------------- #
# Footprint placement (from a `.kicad_mod` lib def)
# --------------------------------------------------------------------------- #


def fetch_footprint_def(mod_path: Path) -> list:
    """Load a `.kicad_mod` file and return its parsed (footprint ...) tree."""
    text = Path(mod_path).read_text(encoding="utf-8", errors="replace")
    data = sexpdata.loads(text)
    if not is_call(data, "footprint"):
        raise ValueError(f"not a footprint file: {mod_path}")
    return data


def get_footprint_rotation(fp: list) -> int:
    """The footprint's own `(at x y rot)` angle, 0 when it carries none."""
    at = find_child(fp, "at")
    if at is None or len(at) < 4:
        return 0
    try:
        return int(round(float(at[3]))) % 360
    except (TypeError, ValueError):
        return 0


def rotate_footprint_pads(fp: list, delta_deg: float) -> int:
    """Add `delta_deg` to every pad's `(at x y [angle])`. Returns pads touched.

    A pad's angle in a `.kicad_pcb` is **absolute**, not relative to the
    footprint: KiCAD writes `footprint_angle + pad_local_angle`. Rotating only
    the footprint's own `(at …)` therefore rotates the pad *positions* while
    leaving the pad *bodies* in their original orientation, which silently
    shorts neighbouring lands on any fine-pitch part.
    """
    delta = float(delta_deg) % 360
    touched = 0
    for pad in find_children(fp, "pad"):
        at = find_child(pad, "at")
        if at is None:
            continue
        current = 0.0
        if len(at) >= 4:
            try:
                current = float(at[3])
            except (TypeError, ValueError):
                current = 0.0
        new = (current + delta) % 360
        if delta == 0 and len(at) < 4:
            continue  # nothing to write: an absent angle already means 0
        new_val = int(new) if float(new).is_integer() else round_mm(new)
        if len(at) >= 4:
            at[3] = new_val
        else:
            at.append(new_val)
        touched += 1
    return touched


def _strip_top_fields(fp: list, names: set[str]) -> list:
    """Return children of `fp` (skipping head + name) with given heads removed."""
    return [c for c in fp[2:] if head_of(c) not in names]


def _build_placed_footprint(
    fp_def: list,
    qualified_lib_id: str,
    reference: str,
    value: str,
    x_k: float,
    y_k: float,
    rotation_deg: int,
    layer: str,
) -> list:
    """Construct a placed footprint instance from a lib (footprint ...) def."""
    placed = copy.deepcopy(fp_def)
    # Drop fields that the placed instance owns directly: layer/at/uuid go in
    # our header. Drop version/generator/generator_version since those describe
    # the source lib, not the placed instance.
    core = _strip_top_fields(
        placed,
        {"version", "generator", "generator_version", "layer", "at", "uuid"},
    )

    # Set Reference / Value properties (preserve their (at), (layer), (effects)).
    for c in core:
        if is_call(c, "property"):
            i = property_name_index(c)
            if len(c) <= i + 1:
                continue
            if c[i] == "Reference":
                c[i + 1] = reference
            elif c[i] == "Value":
                c[i + 1] = value

    # A library footprint in the KiCAD 6 format carries `(fp_text reference
    # "REF**" …)` instead of a `(property "Reference" …)`. Written unchanged it
    # lands on the board as an unnamed part that no sync can match, so the
    # legacy nodes are set too.
    _set_fp_text(core, "reference", reference)
    _set_fp_text(core, "value", value)

    header: list[Any] = [
        [sym("layer"), layer],
        [sym("uuid"), str(uuid.uuid4())],
        [sym("at"), round_mm(x_k), round_mm(y_k), rotation_deg],
    ]
    placed_node = [sym("footprint"), qualified_lib_id, *header, *core]
    # Pad angles are absolute — see `rotate_footprint_pads`.
    if rotation_deg:
        rotate_footprint_pads(placed_node, rotation_deg)
    return placed_node


def add_footprint(
    tree: list,
    *,
    qualified_lib_id: str,
    reference: str,
    value: str,
    x_mm: float,
    y_mm: float,
    rotation: float = 0,
    layer: str = "F.Cu",
    fp_def_node: list,
) -> list:
    """Place a footprint on the PCB. Returns the new (footprint ...) node."""
    if find_footprint_by_reference(tree, reference) is not None:
        raise ValueError(f"footprint reference {reference!r} already exists")
    if layer not in ("F.Cu", "B.Cu"):
        raise ValueError(f"layer must be 'F.Cu' or 'B.Cu' (got {layer!r})")

    xk, yk = pcb_to_file_xy(x_mm, y_mm)
    rot = normalize_rotation(rotation)

    placed = _build_placed_footprint(
        fp_def_node,
        qualified_lib_id=qualified_lib_id,
        reference=reference,
        value=value,
        x_k=xk,
        y_k=yk,
        rotation_deg=rot,
        layer=layer,
    )
    tree.append(placed)
    return placed


def set_footprint_property(fp: list, name: str, value: str) -> bool:
    """Set a footprint's `(property name value)`, adding it hidden if absent.

    Returns True when the file changed. A field KiCAD did not put there —
    `MPN`, `Manufacturer`, an order code — is metadata for the fab, so a new
    one is written hidden on `F.Fab`, which is where KiCAD's own update puts
    the fields it copies from the schematic.
    """
    for prop in find_children(fp, "property"):
        i = property_name_index(prop)
        if len(prop) > i + 1 and prop[i] == name:
            if str(prop[i + 1]) == value:
                return False
            prop[i + 1] = value
            return True

    # No property — a KiCAD 6 footprint spells Reference/Value as `fp_text`.
    # Write there rather than adding a second, conflicting field.
    legacy = {"Reference": "reference", "Value": "value"}.get(name)
    if legacy is not None and get_fp_text(fp, legacy) is not None:
        if get_fp_text(fp, legacy) == value:
            return False
        return _set_fp_text(fp, legacy, value)

    node = [
        sym("property"),
        name,
        value,
        [sym("at"), 0, 0, 0],
        [sym("unlocked"), sym("yes")],
        [sym("layer"), "F.Fab"],
        [sym("hide"), sym("yes")],
        [sym("uuid"), str(uuid.uuid4())],
        [
            sym("effects"),
            [sym("font"), [sym("size"), 1.0, 1.0], [sym("thickness"), 0.15]],
        ],
    ]
    last_prop_idx = max(
        (i for i, c in enumerate(fp) if is_call(c, "property")),
        default=1,
    )
    fp.insert(last_prop_idx + 1, node)
    return True


def remove_footprint(tree: list, reference: str) -> bool:
    for i, child in enumerate(tree):
        if is_call(child, "footprint") and get_footprint_reference(child) == reference:
            tree.pop(i)
            return True
    return False


def move_footprint(
    tree: list,
    reference: str,
    x_mm: float,
    y_mm: float,
    rotation: float | None = None,
    layer: str | None = None,
) -> None:
    fp = find_footprint_by_reference(tree, reference)
    if fp is None:
        raise KeyError(f"no footprint with reference {reference!r}")
    if layer is not None and layer not in ("F.Cu", "B.Cu"):
        raise ValueError(f"layer must be 'F.Cu' or 'B.Cu' (got {layer!r})")

    xk, yk = pcb_to_file_xy(x_mm, y_mm)
    old_rot = get_footprint_rotation(fp)
    at = find_child(fp, "at")
    if at is None:
        # Insert one at the right position (after layer/uuid). Fallback: just append.
        at = [sym("at"), round_mm(xk), round_mm(yk)]
        fp.insert(2, at)
    else:
        at[1] = round_mm(xk)
        at[2] = round_mm(yk)
    if rotation is not None:
        rot = normalize_rotation(rotation)
        if len(at) >= 4:
            at[3] = rot
        else:
            at.append(rot)
        # Pad angles are absolute, so they must follow the footprint —
        # otherwise the pad positions rotate and the pad bodies do not.
        rotate_footprint_pads(fp, rot - old_rot)

    if layer is not None:
        layer_node = find_child(fp, "layer")
        if layer_node:
            layer_node[1] = layer


def place_footprints_grid(
    tree: list,
    spacing_mm: float = 10.0,
    columns: int = 5,
    origin_mcp: tuple[float, float] = (15.0, 15.0),
    unplaced_threshold_mm: float = 0.5,
    only_unplaced: bool = True,
) -> dict:
    """Lay footprints out on a grid, sorted by reference.

    `only_unplaced` (the default) moves just the footprints sitting within
    `unplaced_threshold_mm` of (0, 0) — where KiCAD drops a part it has no
    position for. Pass False to re-arrange every footprint on the board, which
    is what `update_pcb_from_schematic` leaves behind: it places new parts in a
    row below the outline, not at the origin.

    Sorted by reference (R1, R2, …, C1, C2, …) so prefix groups stay contiguous.
    """

    unplaced: list[list] = []
    for fp in iter_footprints(tree):
        if not only_unplaced:
            unplaced.append(fp)
            continue
        at = find_child(fp, "at")
        if at is None:
            unplaced.append(fp)
            continue
        x = float(at[1]) if len(at) > 1 else 0.0
        y = float(at[2]) if len(at) > 2 else 0.0
        if abs(x) <= unplaced_threshold_mm and abs(y) <= unplaced_threshold_mm:
            unplaced.append(fp)

    unplaced.sort(key=lambda fp: get_footprint_reference(fp) or "?")

    placed = 0
    for i, fp in enumerate(unplaced):
        col = i % columns
        row = i // columns
        x_mcp = origin_mcp[0] + col * spacing_mm
        y_mcp = origin_mcp[1] + row * spacing_mm
        xk, yk = pcb_to_file_xy(x_mcp, y_mcp)
        at = find_child(fp, "at")
        if at is None:
            fp.insert(2, [sym("at"), round_mm(xk), round_mm(yk), 0])
        else:
            at[1] = round_mm(xk)
            at[2] = round_mm(yk)
        placed += 1

    return {"placed": placed, "spacing_mm": spacing_mm, "columns": columns}


# --------------------------------------------------------------------------- #
# Tracks / vias
# --------------------------------------------------------------------------- #


def add_track(
    tree: list,
    x1_mm: float,
    y1_mm: float,
    x2_mm: float,
    y2_mm: float,
    width_mm: float = 0.25,
    layer: str = "F.Cu",
    net: int | str = 0,
) -> list:
    """Add a track segment. `net` is a net name, or a legacy integer index."""
    net_value = net_ref(tree, net)
    x1k, y1k = pcb_to_file_xy(x1_mm, y1_mm)
    x2k, y2k = pcb_to_file_xy(x2_mm, y2_mm)
    node = [
        sym("segment"),
        [sym("start"), round_mm(x1k), round_mm(y1k)],
        [sym("end"), round_mm(x2k), round_mm(y2k)],
        [sym("width"), width_mm],
        [sym("layer"), layer],
        [sym("net"), net_value],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_via_array_along_line(
    tree: list,
    *,
    start_mm: tuple[float, float],
    end_mm: tuple[float, float],
    spacing_mm: float = 2.5,
    drill_mm: float = 0.3,
    diameter_mm: float = 0.6,
    perpendicular_offset_mm: float = 0.0,
    net_name: str | None = None,
) -> list[list]:
    """Place a row of vias along a line, optionally offset perpendicular.

    Used for RF ground stitching (call once with `+offset` and once with
    `-offset` to fence both sides of a trace) and for via stitching of
    ground planes.

    Returns the list of new (via ...) nodes appended to the tree.
    """
    if spacing_mm <= 0:
        raise ValueError("spacing_mm must be positive")

    sx, sy = start_mm
    ex, ey = end_mm
    dx, dy = ex - sx, ey - sy
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1e-6:
        raise ValueError("start and end coincide")

    ux, uy = dx / length, dy / length
    # Perpendicular (math CCW)
    px, py = -uy, ux

    net_value = net_ref(tree, net_name) if net_name else 0

    n = max(1, int(length / spacing_mm) + 1)
    new_nodes: list[list] = []
    for i in range(n):
        t = (i / max(1, n - 1)) * length if n > 1 else 0.0
        cx = sx + ux * t + px * perpendicular_offset_mm
        cy = sy + uy * t + py * perpendicular_offset_mm
        xk, yk = pcb_to_file_xy(cx, cy)
        node = [
            sym("via"),
            [sym("at"), round_mm(xk), round_mm(yk)],
            [sym("size"), round_mm(diameter_mm)],
            [sym("drill"), round_mm(drill_mm)],
            [sym("layers"), "F.Cu", "B.Cu"],
            [sym("net"), net_value],
            [sym("uuid"), _uuid()],
        ]
        tree.append(node)
        new_nodes.append(node)
    return new_nodes


def add_via(
    tree: list,
    x_mm: float,
    y_mm: float,
    drill_mm: float = 0.4,
    diameter_mm: float = 0.8,
    net: int | str = 0,
    layers: tuple[str, str] = ("F.Cu", "B.Cu"),
) -> list:
    """Add a via. `net` is a net name, or a legacy integer index."""
    net_value = net_ref(tree, net)
    xk, yk = pcb_to_file_xy(x_mm, y_mm)
    node = [
        sym("via"),
        [sym("at"), round_mm(xk), round_mm(yk)],
        [sym("size"), diameter_mm],
        [sym("drill"), drill_mm],
        [sym("layers"), layers[0], layers[1]],
        [sym("net"), net_value],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


# --------------------------------------------------------------------------- #
# Net inspection / diff pairs / trace length
# --------------------------------------------------------------------------- #


def read_net_node(node: list) -> tuple[int | None, str]:
    """Read a `(net ...)` child of `node` as (index, name).

    Three spellings exist in the wild:
      `(net 3 "GND")` — the table entry and, before KiCAD 10, every pad;
      `(net "GND")`   — KiCAD 10's pads, zones, tracks and vias;
      `(net 3)`       — an index-only reference into the table.
    A missing or unreadable node reads as (None, "").
    """
    net_node = find_child(node, "net")
    if not net_node or len(net_node) < 2:
        return None, ""
    first = net_node[1]
    if isinstance(first, int):
        name = str(net_node[2]) if len(net_node) > 2 else ""
        return first, normalize_name(name)
    return None, normalize_name(str(first))


def net_of(node: list, tree: list | None = None) -> str:
    """The net a pad / zone / track / via belongs to, by name.

    Empty for the unconnected net. A KiCAD 10 zone carries only `(net "GND")`;
    a zone this server wrote before that carries `(net 0) (net_name "GND")`,
    so the `net_name` child is read as a fallback. Pass `tree` for a board that
    still has a net table: its tracks reference nets by index alone, and only
    the table can name them.
    """
    index, name = read_net_node(node)
    if name:
        return name
    net_name_node = find_child(node, "net_name")
    if net_name_node and len(net_name_node) >= 2 and str(net_name_node[1]):
        return normalize_name(str(net_name_node[1]))
    if index:
        if tree is not None:
            for entry in _net_table(tree):
                if entry["index"] == index:
                    return entry["name"]
        return f"#{index}"  # index-only reference, no table entry to name it
    return ""


def _net_table(tree: list) -> list[dict]:
    """The top-level `(net N "name")` declarations, if the board has any.

    KiCAD 10 (board format 20260206) stopped writing this table: pads, zones
    and tracks name their net directly. Older boards still carry it, and their
    items reference nets by index, so it is read when present.
    """
    out: list[dict] = []
    for n in tree[1:]:
        if is_call(n, "net") and len(n) >= 3 and isinstance(n[1], int):
            out.append({"index": int(n[1]), "name": normalize_name(str(n[2]))})
    return out


def has_net_table(tree: list) -> bool:
    """True when the board declares real nets in a top-level table.

    A lone `(net 0 "")` does not count: that is the unconnected-net
    placeholder, which older templates carry and which KiCAD 10 drops on its
    first save. Treating it as a table made every write on a fresh project take
    the index branch and emit the pre-KiCAD-10 spelling.
    """
    return any(e["index"] != 0 or e["name"] for e in _net_table(tree))


def list_nets(tree: list) -> list[dict]:
    """Every net on the board as [{index, name}], sorted by name.

    Read from the top-level table when the board has one, and otherwise from
    the items themselves — a KiCAD 10 board declares its nets nowhere else.
    `index` is None for a net that has no table entry. The unconnected net
    (empty name) is not listed.

    Names are decoded for display: a net stored `VBUS{slash}5V` is reported
    as `VBUS/5V`. The tree keeps the stored form.
    """
    by_name: dict[str, int | None] = {}
    for entry in _net_table(tree):
        if entry["name"]:
            by_name.setdefault(entry["name"], entry["index"])

    def _note(node: list) -> None:
        name = net_of(node)
        if name:
            by_name.setdefault(name, None)

    for node in tree[1:]:
        if is_call(node, "footprint"):
            for pad in find_children(node, "pad"):
                _note(pad)
        elif is_call(node, "zone") or is_call(node, "segment") or is_call(node, "arc") or is_call(node, "via"):
            _note(node)

    return [{"index": idx, "name": name} for name, idx in sorted(by_name.items())]


def find_net_index(tree: list, net_name: str) -> int | None:
    """Index of a net by name, or None when the board has no table entry for it.

    A KiCAD 10 board has no table at all, so this returns None for every net.
    Use `net_exists` to ask whether a net is on the board, and `net_ref` to get
    the value to write into a new `(net ...)` node.
    """
    wanted = normalize_name(net_name)
    for entry in _net_table(tree):
        if entry["name"] == wanted:
            return entry["index"]
    return None


def net_exists(tree: list, net_name: str) -> bool:
    """True when `net_name` is used anywhere on the board."""
    wanted = normalize_name(net_name)
    return any(n["name"] == wanted for n in list_nets(tree))


def net_ref(tree: list, net: int | str | None) -> int | str:
    """The value to write into a new `(net ...)` node for `net`.

    Accepts a net name (preferred) or a legacy integer index. Returns the
    board's own spelling: an index on a board that still has a net table, the
    name on a KiCAD 10 board. `None`, `0` and `""` mean the unconnected net.

    Raises KeyError for a name the board does not know — a track on a net that
    exists nowhere else is invisible to KiCAD and to every check here.
    """
    if net is None or net == "" or net == 0:
        return 0
    if isinstance(net, int):
        return net
    name = normalize_name(str(net))
    idx = find_net_index(tree, name)
    if idx is not None:
        return idx
    if not net_exists(tree, name):
        known = [n["name"] for n in list_nets(tree)]
        raise KeyError(f"net {net!r} not found on this board; known nets: {known}")
    return name


# Suffix conventions for diff pair members. Order matters: more-specific first.
_DIFF_PATTERNS: list[tuple[str, str, str]] = [
    # (positive_suffix, negative_suffix, conjunction explainer)
    ("_P", "_N", "_P/_N"),
    ("+", "-", "+/−"),
    ("DP", "DM", "DP/DM (USB-style)"),
    ("_p", "_n", "_p/_n (lowercase)"),
]


def find_diff_pair_candidates(tree: list) -> list[dict]:
    """Detect pairs of nets that look like differential pairs by name.

    Returns a list of dicts with `base_name`, `p`, `n`, and `convention`.
    Skips nets that don't have a partner present.
    """
    names = {n["name"] for n in list_nets(tree) if n["name"]}
    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for name in sorted(names):
        for p_suf, n_suf, label in _DIFF_PATTERNS:
            if name.endswith(p_suf) and len(name) > len(p_suf):
                base = name[: -len(p_suf)]
                partner = base + n_suf
                if partner in names:
                    key = tuple(sorted((name, partner)))
                    if key in seen:
                        continue
                    seen.add(key)
                    # Strip trailing separator from the displayed base name
                    # ("USB_DP" -> "USB", not "USB_").
                    display_base = base.rstrip("_-.")
                    pairs.append({
                        "base_name": display_base,
                        "p": name,
                        "n": partner,
                        "convention": label,
                    })
                    break  # only emit each net once
    return pairs


def compute_trace_length(tree: list, net_name: str) -> dict:
    """Sum lengths of every (segment ...) on the named net.

    Returns mm total plus per-layer breakdown. Multi-layer traces are
    counted across all layers; vias add zero length.
    """
    wanted = normalize_name(net_name)
    if not net_exists(tree, wanted):
        raise KeyError(f"unknown net {net_name!r}")
    idx = find_net_index(tree, wanted)
    total = 0.0
    by_layer: dict[str, float] = {}
    seg_count = 0
    for seg in find_children(tree, "segment"):
        seg_index, seg_name = read_net_node(seg)
        if seg_name:
            if seg_name != wanted:
                continue
        elif idx is None or seg_index != idx:
            continue
        start = find_child(seg, "start")
        end = find_child(seg, "end")
        if not (start and end and len(start) >= 3 and len(end) >= 3):
            continue
        seg_len = math.hypot(
            float(end[1]) - float(start[1]),
            float(end[2]) - float(start[2]),
        )
        total += seg_len
        layer_node = find_child(seg, "layer")
        layer_name = layer_node[1] if layer_node and len(layer_node) >= 2 else ""
        by_layer[layer_name] = by_layer.get(layer_name, 0.0) + seg_len
        seg_count += 1
    return {
        "net": net_name,
        "total_mm": round_mm(total),
        "segment_count": seg_count,
        "by_layer_mm": {k: round_mm(v) for k, v in by_layer.items()},
    }


def add_meander_segments(
    tree: list,
    *,
    start_mm: tuple[float, float],
    end_mm: tuple[float, float],
    target_length_mm: float,
    amplitude_mm: float = 1.5,
    side: str = "up",
    width_mm: float = 0.25,
    layer: str = "F.Cu",
    net_name: str | None = None,
    base_width_mm: float | None = None,
) -> list[list]:
    """Generate a meander between two points and emit (segment ...) nodes.

    `side`: "up" / "down" (perpendicular direction). `net_name` resolves to
    the net index; pass None for net 0 (default unconnected). Returns the
    list of new segment nodes appended to the tree.
    """
    side_map = {"up": 1, "down": -1, "left": 1, "right": -1}
    if side not in side_map:
        raise ValueError(f"side must be 'up' or 'down' (got {side!r})")

    waypoints = length_tuning.generate_meander(
        start_mm, end_mm, target_length_mm,
        amplitude_mm=amplitude_mm,
        side=side_map[side],
        base_width_mm=base_width_mm,
    )
    net_value = net_ref(tree, net_name) if net_name else 0

    new_segments: list[list] = []
    for i in range(len(waypoints) - 1):
        x1m, y1m = waypoints[i]
        x2m, y2m = waypoints[i + 1]
        x1k, y1k = pcb_to_file_xy(x1m, y1m)
        x2k, y2k = pcb_to_file_xy(x2m, y2m)
        node = [
            sym("segment"),
            [sym("start"), round_mm(x1k), round_mm(y1k)],
            [sym("end"), round_mm(x2k), round_mm(y2k)],
            [sym("width"), round_mm(width_mm)],
            [sym("layer"), layer],
            [sym("net"), net_value],
            [sym("uuid"), _uuid()],
        ]
        tree.append(node)
        new_segments.append(node)
    return new_segments


def _uuid() -> str:
    import uuid as _u
    return str(_u.uuid4())


# --------------------------------------------------------------------------- #
# Copper zones (pours)
# --------------------------------------------------------------------------- #


def get_board_outline_polygon_kicad(tree: list) -> list[tuple[float, float]] | None:
    """Extract the board outline as KiCAD-coord (x, y) points.

    Currently supports rectangular outlines (`gr_rect` on Edge.Cuts), since
    that's what `set_board_outline` produces. Returns None if no rectangular
    Edge.Cuts is found.
    """
    for node in find_children(tree, "gr_rect"):
        layer = find_child(node, "layer")
        if not layer or len(layer) < 2 or layer[1] != "Edge.Cuts":
            continue
        start = find_child(node, "start")
        end = find_child(node, "end")
        if not (start and end and len(start) >= 3 and len(end) >= 3):
            continue
        x1, y1 = float(start[1]), float(start[2])
        x2, y2 = float(end[1]), float(end[2])
        # KiCAD doesn't care about winding order.
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return None


def add_zone(
    tree: list,
    *,
    net_name: str,
    layer: str,
    polygon_mcp: list[tuple[float, float]],
    fill_clearance_mm: float = 0.5,
    min_thickness_mm: float = 0.25,
    thermal_gap_mm: float = 0.5,
    thermal_bridge_mm: float = 0.5,
    name: str = "",
    priority: int = 0,
) -> list:
    """Add a filled copper zone for `net_name` on `layer` covering `polygon_mcp`.

    Polygon points are in KiCAD coordinates (Y down). The zone is declared as
    `(fill yes)` so KiCAD's DRC will compute the filled regions on demand
    (refill via `kicad-cli pcb drc --refill-zones` or in the GUI).

    `layer` may be a single copper layer ("F.Cu", "B.Cu", "In1.Cu", …) or
    a glob like "*.Cu" for every copper layer.
    """
    if not polygon_mcp or len(polygon_mcp) < 3:
        raise ValueError("polygon needs at least 3 points")

    # Resolve the net the way the board spells nets. On a board with a legacy
    # table that is an index — allocate an entry when the net has none. On a
    # KiCAD 10 board it is the name itself; net 0 is the unconnected net, so a
    # named zone must never be written with index 0.
    net_nodes: list[list]
    if net_name and has_net_table(tree):
        idx = find_net_index(tree, net_name)
        if idx is None:
            idx = max((e["index"] for e in _net_table(tree)), default=-1) + 1
            tree.insert(_first_child_index(tree, "footprint", default=len(tree)),
                        [sym("net"), idx, net_name])
        net_nodes = [[sym("net"), idx], [sym("net_name"), net_name]]
    elif net_name:
        net_nodes = [[sym("net"), net_name]]
    else:
        net_nodes = [[sym("net"), 0]]

    pts_kicad = [pcb_to_file_xy(x, y) for (x, y) in polygon_mcp]
    pts_block: list[Any] = [sym("pts")]
    for x, y in pts_kicad:
        pts_block.append([sym("xy"), round_mm(x), round_mm(y)])

    # Decide layer node: `layer` for single, `layers` for multiple
    if "*" in layer:
        layer_node = [sym("layers"), layer]
    else:
        layer_node = [sym("layer"), layer]

    zone_node = [
        sym("zone"),
        *net_nodes,
        layer_node,
        [sym("uuid"), str(uuid.uuid4())],
        [sym("name"), name or f"{net_name}_{layer}"],
        [sym("hatch"), sym("edge"), 0.5],
        [sym("priority"), priority],
        [sym("connect_pads"), [sym("clearance"), round_mm(fill_clearance_mm)]],
        [sym("min_thickness"), round_mm(min_thickness_mm)],
        [sym("filled_areas_thickness"), sym("no")],
        [
            sym("fill"),
            sym("yes"),
            [sym("thermal_gap"), round_mm(thermal_gap_mm)],
            [sym("thermal_bridge_width"), round_mm(thermal_bridge_mm)],
            [sym("smoothing"), sym("none")],
            [sym("radius"), 1.0],
            [sym("island_removal_mode"), 0],
            [sym("island_area_min"), 10.0],
        ],
        [sym("polygon"), pts_block],
    ]
    tree.append(zone_node)
    return zone_node


def add_ground_plane(
    tree: list,
    *,
    layer: str = "B.Cu",
    net_name: str = "GND",
    fill_clearance_mm: float = 0.5,
) -> list:
    """Convenience: pour a ground plane on `layer` covering the whole board."""
    poly_kicad = get_board_outline_polygon_kicad(tree)
    if poly_kicad is None:
        raise RuntimeError(
            "no board outline found; call set_board_outline first so the zone "
            "knows what area to fill."
        )
    poly_mcp = [file_to_pcb_xy(x, y) for (x, y) in poly_kicad]
    return add_zone(
        tree,
        net_name=net_name,
        layer=layer,
        polygon_mcp=poly_mcp,
        fill_clearance_mm=fill_clearance_mm,
        name=f"{net_name}_{layer}",
    )


def _first_child_index(tree: list, head: str, default: int) -> int:
    for i, c in enumerate(tree[1:], start=1):
        if is_call(c, head):
            return i
    return default


# --------------------------------------------------------------------------- #
# Silk / fab text
# --------------------------------------------------------------------------- #


def add_silk_text(
    tree: list,
    *,
    text: str,
    x_mm: float,
    y_mm: float,
    layer: str = "F.SilkS",
    size_mm: float = 1.0,
    rotation: float = 0,
    thickness_mm: float | None = None,
) -> list:
    """Add a `(gr_text ...)` to the PCB, default on F.SilkS.

    Common layers: F.SilkS, B.SilkS, F.Fab, B.Fab, F.Cu, B.Cu (text on copper).
    """
    if thickness_mm is None:
        thickness_mm = round_mm(size_mm * 0.15)
    rot = normalize_rotation(rotation)
    xk, yk = pcb_to_file_xy(x_mm, y_mm)
    node = [
        sym("gr_text"),
        text,
        [sym("at"), round_mm(xk), round_mm(yk), rot],
        [sym("layer"), layer],
        [sym("uuid"), str(uuid.uuid4())],
        [
            sym("effects"),
            [
                sym("font"),
                [sym("size"), round_mm(size_mm), round_mm(size_mm)],
                [sym("thickness"), round_mm(thickness_mm)],
            ],
        ],
    ]
    tree.append(node)
    return node


# --------------------------------------------------------------------------- #
# List / export
# --------------------------------------------------------------------------- #


def list_footprints_summary(tree: list) -> list[dict]:
    out = []
    for fp in iter_footprints(tree):
        ref = get_footprint_reference(fp)
        value = _footprint_property(fp, "Value")
        layer_node = find_child(fp, "layer")
        layer = layer_node[1] if layer_node and len(layer_node) >= 2 else ""
        at = find_child(fp, "at")
        x_k = float(at[1]) if at and len(at) > 1 else 0.0
        y_k = float(at[2]) if at and len(at) > 2 else 0.0
        rot = float(at[3]) if at and len(at) > 3 else 0.0
        x_mcp, y_mcp = file_to_pcb_xy(x_k, y_k)
        out.append(
            {
                "reference": ref or "?",
                "value": value or "",
                "lib_id": fp[1] if len(fp) > 1 and isinstance(fp[1], str) else "",
                "layer": layer,
                "position_mm": [round_mm(x_mcp), round_mm(y_mcp)],
                "rotation": rot,
            }
        )
    return out
