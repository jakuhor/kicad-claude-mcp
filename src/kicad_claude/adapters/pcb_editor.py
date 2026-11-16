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

from kicad_claude.adapters import length_tuning, pcb_geometry, pcb_layers
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
    if shape != "rect":
        raise ValueError(f"only 'rect' outlines are supported (got {shape!r})")

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


def _frame_point(x: float, y: float, frame: tuple[float, float, float]) -> tuple[float, float]:
    """A footprint-local point placed in `frame` (x, y, CCW angle, Y down)."""
    fx, fy, angle = frame
    r = math.radians(angle)
    ca, sa = math.cos(r), math.sin(r)
    return fx + x * ca + y * sa, fy - x * sa + y * ca


def _unframe_point(x: float, y: float, frame: tuple[float, float, float]) -> tuple[float, float]:
    """Inverse of `_frame_point`: a board point back to footprint-local."""
    fx, fy, angle = frame
    dx, dy = x - fx, y - fy
    r = math.radians(angle)
    ca, sa = math.cos(r), math.sin(r)
    return dx * ca - dy * sa, dx * sa + dy * ca


def move_footprint_zones(fp: list, old: tuple[float, float, float],
                         new: tuple[float, float, float]) -> int:
    """Carry a footprint's own zones from frame `old` to frame `new`.

    Unlike pads and graphics, a zone inside a placed footprint is stored in
    **board** coordinates (KiCAD's `FOOTPRINT::Move` / `Rotate` move it with
    the part). A library `.kicad_mod` holds it footprint-local, i.e. in frame
    (0, 0, 0). Left untouched, an antenna keep-out stays at the board origin
    and DRC reports `lib_footprint_mismatch`. Returns the points moved.
    """
    if old == new:
        return 0
    moved = 0
    for zone in find_children(fp, "zone"):
        for kind in ("polygon", "filled_polygon"):
            for poly in find_children(zone, kind):
                pts = find_child(poly, "pts")
                for xy in find_children(pts, "xy") if pts else []:
                    lx, ly = _unframe_point(float(xy[1]), float(xy[2]), old)
                    nx, ny = _frame_point(lx, ly, new)
                    xy[1], xy[2] = round_mm(nx), round_mm(ny)
                    moved += 1
    return moved


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

    at_node: list[Any] = [sym("at"), round_mm(x_k), round_mm(y_k)]
    if rotation_deg:  # KiCAD omits a zero angle
        at_node.append(rotation_deg)
    header: list[Any] = [
        [sym("layer"), layer],
        [sym("uuid"), str(uuid.uuid4())],
        at_node,
    ]
    placed_node = [sym("footprint"), qualified_lib_id, *header, *core]
    # Pad angles are absolute — see `rotate_footprint_pads`.
    if rotation_deg:
        rotate_footprint_pads(placed_node, rotation_deg)
    # Footprint zones are stored in board coordinates.
    move_footprint_zones(placed_node, (0.0, 0.0, 0.0),
                         (float(round_mm(x_k)), float(round_mm(y_k)), float(rotation_deg)))
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


# Footprint `(attr ...)` flags in the order KiCAD writes them
# (pcb_io_kicad_sexpr.cpp, FormatFootprint).
_ATTR_ORDER = ("smd", "through_hole", "board_only", "exclude_from_pos_files",
               "exclude_from_bom", "exclude_from_sim", "allow_missing_courtyard",
               "dnp", "allow_soldermask_bridges")
# Where a new `(attr)` goes: before the first of these children.
_AFTER_ATTR = ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_curve",
               "fp_text", "fp_text_box", "dimension", "pad", "zone", "group",
               "embedded_fonts", "model")


def set_footprint_attr_flags(fp: list, flags: dict[str, bool]) -> list[str]:
    """Set or clear footprint attribute flags (`exclude_from_bom`, `dnp`, …).

    Returns the flags that changed. Keeps KiCAD's token order, drops an
    `(attr)` left empty and adds one where KiCAD puts it when needed.
    """
    attr = find_child(fp, "attr")
    current = [str(t) for t in attr[1:]] if attr else []
    changed = []
    for name, on in flags.items():
        if name not in _ATTR_ORDER:
            raise ValueError(f"unknown footprint attribute {name!r}")
        if on and name not in current:
            current.append(name)
            changed.append(name)
        elif not on and name in current:
            current.remove(name)
            changed.append(name)
    if not changed:
        return []
    known = [t for t in _ATTR_ORDER if t in current]
    tokens = known + [t for t in current if t not in _ATTR_ORDER]
    if attr is not None:
        if tokens:
            attr[1:] = [sym(t) for t in tokens]
        else:
            fp.remove(attr)
    elif tokens:
        idx = next((i for i, c in enumerate(fp) if head_of(c) in _AFTER_ATTR), len(fp))
        fp.insert(idx, [sym("attr"), *[sym(t) for t in tokens]])
    return changed


def remove_footprint(tree: list, reference: str) -> bool:
    for i, child in enumerate(tree):
        if is_call(child, "footprint") and get_footprint_reference(child) == reference:
            tree.pop(i)
            return True
    return False


# Board item kinds `remove_board_items` removes, by s-expression head.
REMOVABLE_KINDS = {"track": "segment", "arc": "arc", "via": "via", "zone": "zone",
                   "text": "gr_text"}


def _item_layers(node: list) -> list[str]:
    layers = find_child(node, "layers") or find_child(node, "layer")
    return [str(x) for x in layers[1:]] if layers else []


def remove_board_items(
    tree: list,
    *,
    item_uuid: str = "",
    net: str = "",
    kinds: list[str] | tuple[str, ...] = ("track", "arc", "via"),
    layer: str = "",
    dry_run: bool = False,
) -> list[dict]:
    """Remove routing items by uuid, or every item of `kinds` on `net`.

    `layer` narrows a net removal to tracks, arcs and zones on that layer;
    vias are kept then, since they join that layer to others. Returns one
    `{kind, uuid, net, layers}` per removed item (all matches with `dry_run`).
    """
    if not item_uuid and not net:
        raise ValueError("pass a uuid or a net; removing every item is not supported")
    unknown = sorted(set(kinds) - set(REMOVABLE_KINDS))
    if unknown:
        raise ValueError(f"unknown kind(s) {unknown}; use {sorted(REMOVABLE_KINDS)}")
    heads = {REMOVABLE_KINDS[k]: k for k in kinds}
    want_net = normalize_name(net) if net else ""
    matched: list[tuple[int, dict]] = []
    for i, node in enumerate(tree):
        if not isinstance(node, list) or head_of(node) not in (
                heads if not item_uuid else REMOVABLE_KINDS.values()):
            continue
        u = find_child(node, "uuid")
        uid = str(u[1]) if u and len(u) >= 2 else ""
        if item_uuid:
            if uid != item_uuid:
                continue
        else:
            if normalize_name(net_of(node, tree)) != want_net:
                continue
            if layer and (head_of(node) == "via" or layer not in _item_layers(node)):
                continue
        kind = {v: k for k, v in REMOVABLE_KINDS.items()}[head_of(node)]
        matched.append((i, {"kind": kind, "uuid": uid, "net": net_of(node, tree),
                            "layers": _item_layers(node)}))
    if item_uuid and not matched:
        raise KeyError(f"no track, arc, via, zone or text with uuid {item_uuid!r} on this board")
    if not dry_run:
        for i, _ in reversed(matched):
            del tree[i]
    return [m for _, m in matched]


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
    old_frame = ((float(at[1]), float(at[2]), float(old_rot)) if at is not None and len(at) >= 3
                 else (0.0, 0.0, float(old_rot)))
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
    move_footprint_zones(fp, old_frame, (float(at[1]), float(at[2]),
                                         float(get_footprint_rotation(fp))))

    if layer is not None:
        layer_node = find_child(fp, "layer")
        if layer_node:
            layer_node[1] = layer


_SIDES = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


def _fp_box(tree: list, fp: list) -> list[float]:
    """Courtyard box on the board; pads' centres +-0.5 mm when it has none."""
    box = footprint_courtyard_bbox(fp)
    if box is not None:
        return box
    from kicad_claude.adapters import pcb_netlist

    ref = get_footprint_reference(fp)
    pts = [p["point"] for p in pcb_netlist.list_pads(tree) if p["ref"] == ref]
    if not pts:
        at = find_child(fp, "at")
        x, y = file_to_pcb_xy(float(at[1]), float(at[2]))
        return [x - 0.5, y - 0.5, x + 0.5, y + 0.5]
    xs, ys = [float(p[0]) for p in pts], [float(p[1]) for p in pts]
    return [min(xs) - 0.5, min(ys) - 0.5, max(xs) + 0.5, max(ys) + 0.5]


def place_near(tree: list, refs: list[str], anchor: str, side: str, *,
               gap_mm: float = 0.25, rotation: float | None = None,
               grid_mm: float = 0.05) -> list[dict]:
    """Put footprints beside `anchor` ("REF", or "REF.PAD" to line up with
    that pad) on `side` (left / right / top / bottom), courtyard to
    courtyard `gap_mm` apart. Several refs stack along that side in order,
    the first centred on the anchor (or its pad). Positions snap to
    `grid_mm`. Returns [{ref, x_mm, y_mm}]."""
    if side not in _SIDES:
        raise ValueError(f"side must be one of {list(_SIDES)}, got {side!r}")
    a_ref, _, a_pad = anchor.partition(".")
    a_fp = find_footprint_by_reference(tree, a_ref)
    if a_fp is None:
        raise KeyError(f"no footprint {a_ref!r}")
    abox = _fp_box(tree, a_fp)
    centre = ((abox[0] + abox[2]) / 2, (abox[1] + abox[3]) / 2)
    if a_pad:
        from kicad_claude.adapters import pcb_netlist

        pad = next((p for p in pcb_netlist.list_pads(tree)
                    if p["ref"] == a_ref and p["pad"] == a_pad), None)
        if pad is None:
            raise KeyError(f"no pad {anchor!r}")
        centre = (float(pad["point"][0]), float(pad["point"][1]))
    dx, dy = _SIDES[side]

    def snap(v: float) -> float:
        return round(round(v / grid_mm) * grid_mm, 4) if grid_mm > 0 else round(v, 4)

    out = []
    cursor = None  # running edge along the side
    for ref in refs:
        if ref == a_ref:
            raise ValueError(f"{ref} is the anchor")
        fp = find_footprint_by_reference(tree, ref)
        if fp is None:
            raise KeyError(f"no footprint {ref!r}")
        at = find_child(fp, "at")
        ox, oy = file_to_pcb_xy(float(at[1]), float(at[2]))
        move_footprint(tree, ref, ox, oy, rotation)
        b = _fp_box(tree, fp)
        rel = (b[0] - ox, b[1] - oy, b[2] - ox, b[3] - oy)  # box around the origin
        w, h = rel[2] - rel[0], rel[3] - rel[1]
        if dx:  # left / right: facing edge on x, stack downwards in y
            x = (abox[0] - gap_mm - rel[2]) if dx < 0 else (abox[2] + gap_mm - rel[0])
            top = centre[1] - h / 2 if cursor is None else cursor + gap_mm
            y = top - rel[1]
            cursor = top + h
        else:   # top / bottom: facing edge on y, stack rightwards in x
            y = (abox[1] - gap_mm - rel[3]) if dy < 0 else (abox[3] + gap_mm - rel[1])
            left = centre[0] - w / 2 if cursor is None else cursor + gap_mm
            x = left - rel[0]
            cursor = left + w
        x, y = snap(x), snap(y)
        move_footprint(tree, ref, x, y)
        out.append({"ref": ref, "x_mm": x, "y_mm": y})
    return out


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
            rot = float(get_footprint_rotation(fp))
            old_frame = (float(at[1]), float(at[2]), rot)
            at[1] = round_mm(xk)
            at[2] = round_mm(yk)
            move_footprint_zones(fp, old_frame, (float(at[1]), float(at[2]), rot))
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
        table = {e["index"]: e["name"] for e in _net_table(tree)}
        if net not in table:
            known = [n["name"] for n in list_nets(tree)]
            raise KeyError(
                f"net index {net} is not in this board's net table (a KiCAD 10 "
                f"board has none: pass the net name); known nets: {known}")
        return net
    name = normalize_name(str(net))
    idx = find_net_index(tree, name)
    if idx is not None:
        return idx
    stored = stored_net_name(tree, name)
    if stored is None:
        known = [n["name"] for n in list_nets(tree)]
        raise KeyError(f"net {net!r} not found on this board; known nets: {known}")
    return stored


def stored_net_name(tree: list, net_name: str) -> str | None:
    """The board's own (brace-escaped) spelling of a net, or None if unused.

    Names are compared decoded, but the value returned is the raw string from
    the file: `Net-(U5-NR{slash}SS)`, never `Net-(U5-NR/SS)`. Writing the
    decoded form would create a different net whose bare `/` reads as a
    sheet-path separator.
    """
    wanted = normalize_name(net_name)

    def _raw(node: list) -> str | None:
        n = find_child(node, "net")
        if n and len(n) >= 2:
            v = n[2] if isinstance(n[1], int) and len(n) > 2 else n[1]
            if not isinstance(v, int) and normalize_name(str(v)) == wanted:
                return str(v)
        nn = find_child(node, "net_name")
        if nn and len(nn) >= 2 and normalize_name(str(nn[1])) == wanted:
            return str(nn[1])
        return None

    for node in tree[1:]:
        if is_call(node, "footprint"):
            for pad in find_children(node, "pad"):
                if (r := _raw(pad)) is not None:
                    return r
        elif head_of(node) in ("zone", "segment", "arc", "via"):
            if (r := _raw(node)) is not None:
                return r
    return None


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

    The outer (largest) closed Edge.Cuts loop: a `gr_rect`, a `gr_poly`, a
    `gr_circle`, or `gr_line`/`gr_arc` pieces chained end to end (a rounded
    rectangle). Arcs are flattened to <= 5 degree chords. Returns None when no
    closed Edge.Cuts loop exists.
    """
    loops = pcb_geometry.board_outline_loops(tree)
    return loops[0] if loops else None


# Zone pad connection as `add_zone` takes it -> the token KiCAD writes after
# `connect_pads` (pcb_io_kicad_sexpr.cpp: THERMAL is the default, not written).
PAD_CONNECTIONS = ("thermal", "solid", "thru_hole_only", "none")
_PAD_CONNECTION_TOKEN = {
    "thermal": [],
    "solid": [sym("yes")],
    "thru_hole_only": [sym("thru_hole_only")],
    "none": [sym("no")],
}


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
    pad_connection: str = "thermal",
) -> list:
    """Add a filled copper zone for `net_name` on `layer` covering `polygon_mcp`.

    `pad_connection` is one of PAD_CONNECTIONS: `thermal` (KiCAD's default,
    not written), `solid`, `thru_hole_only` (thermal on THT pads, solid on
    SMD), `none`. A higher `priority` zone is filled first and cut out of the
    lower ones it overlaps.

    Polygon points are in KiCAD coordinates (Y down). The zone is declared as
    `(fill yes)` so KiCAD's DRC will compute the filled regions on demand
    (refill via `kicad-cli pcb drc --refill-zones` or in the GUI).

    `layer` may be a single copper layer ("F.Cu", "B.Cu", "In1.Cu", …) or
    a glob like "*.Cu" for every copper layer.
    """
    if not polygon_mcp or len(polygon_mcp) < 3:
        raise ValueError("polygon needs at least 3 points")
    if pad_connection not in PAD_CONNECTIONS:
        raise ValueError(f"pad_connection must be one of {PAD_CONNECTIONS}")
    if priority < 0:
        raise ValueError("priority must be >= 0")

    # Resolve the net the way the board spells nets. On a board with a legacy
    # table that is an index — allocate an entry when the net has none. On a
    # KiCAD 10 board it is the name itself; net 0 is the unconnected net, so a
    # named zone must never be written with index 0.
    net_nodes: list[list]
    if net_name:  # a caller may type the decoded name; write the stored one
        net_name = stored_net_name(tree, net_name) or net_name
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
        [sym("connect_pads"), *_PAD_CONNECTION_TOKEN[pad_connection],
         [sym("clearance"), round_mm(fill_clearance_mm)]],
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


def find_zones(tree: list, *, item_uuid: str = "", name: str = "", net: str = "",
               layer: str = "") -> list[list]:
    """Zones matching a uuid, or a name, or a net (optionally on one layer)."""
    if not (item_uuid or name or net):
        raise ValueError("pass a zone uuid, name or net")
    out = []
    for node in find_children(tree, "zone"):
        u, n = find_child(node, "uuid"), find_child(node, "name")
        if item_uuid:
            if u and str(u[1]) == item_uuid:
                out.append(node)
            continue
        if name and not (n and str(n[1]) == name):
            continue
        if net and normalize_name(net_of(node, tree)) != normalize_name(net):
            continue
        if layer and layer not in _item_layers(node):
            continue
        out.append(node)
    return out


def set_zone(zone: list, *, pad_connection: str | None = None, priority: int | None = None,
             clearance_mm: float | None = None, min_thickness_mm: float | None = None,
             thermal_gap_mm: float | None = None,
             thermal_bridge_mm: float | None = None) -> list[str]:
    """Change a zone's fill settings in place; returns the keys changed.
    The fill itself is stale afterwards: refill (`run_drc(refill_zones=True)`)."""
    changed = []

    def put(parent: list, head: str, value, before: str | None = None) -> None:
        node = find_child(parent, head)
        if node is None:
            node = [sym(head), value]
            idx = next((i for i, c in enumerate(parent) if before and is_call(c, before)),
                       len(parent))
            parent.insert(idx, node)
        else:
            node[1:] = [value]

    if pad_connection is not None:
        if pad_connection not in PAD_CONNECTIONS:
            raise ValueError(f"pad_connection must be one of {PAD_CONNECTIONS}")
        cp = find_child(zone, "connect_pads")
        if cp is None:
            cp = [sym("connect_pads"), [sym("clearance"), 0.5]]
            zone.insert(len(zone) - 1, cp)
        rest = [c for c in cp[1:] if isinstance(c, list)]
        cp[1:] = list(_PAD_CONNECTION_TOKEN[pad_connection]) + rest
        changed.append("pad_connection")
    if clearance_mm is not None:
        cp = find_child(zone, "connect_pads")
        if cp is None:
            cp = [sym("connect_pads")]
            zone.insert(len(zone) - 1, cp)
        put(cp, "clearance", round_mm(clearance_mm))
        changed.append("clearance_mm")
    if priority is not None:
        if priority < 0:
            raise ValueError("priority must be >= 0")
        put(zone, "priority", int(priority), before="connect_pads")
        changed.append("priority")
    if min_thickness_mm is not None:
        put(zone, "min_thickness", round_mm(min_thickness_mm), before="filled_areas_thickness")
        changed.append("min_thickness_mm")
    fill = find_child(zone, "fill")
    for key, head, value in (("thermal_gap_mm", "thermal_gap", thermal_gap_mm),
                             ("thermal_bridge_mm", "thermal_bridge_width", thermal_bridge_mm)):
        if value is not None:
            if fill is None:
                fill = [sym("fill"), sym("yes")]
                zone.insert(len(zone) - 1, fill)
            put(fill, head, round_mm(value))
            changed.append(key)
    return changed


def edit_text(tree: list, item_uuid: str, *, text: str | None = None,
              x_mm: float | None = None, y_mm: float | None = None,
              rotation: float | None = None, layer: str | None = None,
              size_mm: float | None = None) -> list:
    """Change a board `(gr_text ...)` by uuid: content, position, rotation,
    layer (mirrored on a back layer), size."""
    node = next((n for n in find_children(tree, "gr_text")
                 if (u := find_child(n, "uuid")) and str(u[1]) == item_uuid), None)
    if node is None:
        raise KeyError(f"no board text with uuid {item_uuid!r}")
    if text is not None:
        node[1] = text
    at = find_child(node, "at")
    if x_mm is not None or y_mm is not None:
        xk, yk = pcb_to_file_xy(x_mm if x_mm is not None else file_to_pcb_xy(at[1], at[2])[0],
                                y_mm if y_mm is not None else file_to_pcb_xy(at[1], at[2])[1])
        at[1], at[2] = round_mm(xk), round_mm(yk)
    if rotation is not None:
        rot = normalize_rotation(rotation)
        if len(at) >= 4:
            at[3] = rot
        else:
            at.append(rot)
    effects = find_child(node, "effects")
    if size_mm is not None and effects is not None:
        font = find_child(effects, "font")
        size = find_child(font, "size") if font else None
        if size is not None:
            size[1:] = [round_mm(size_mm), round_mm(size_mm)]
    if layer is not None:
        find_child(node, "layer")[1] = layer
        if effects is not None:
            j = find_child(effects, "justify")
            words = [str(w) for w in j[1:]] if j else []
            want = layer.startswith("B.")
            if want and "mirror" not in words:
                if j is None:
                    effects.append([sym("justify"), sym("mirror")])
                else:
                    j.append(sym("mirror"))
            elif not want and "mirror" in words:
                j[1:] = [w for w in j[1:] if str(w) != "mirror"]
                if len(j) == 1:
                    effects.remove(j)
    return node


def add_ground_plane(
    tree: list,
    *,
    layer: str = "B.Cu",
    net_name: str = "GND",
    fill_clearance_mm: float = 0.5,
    pad_connection: str = "thermal",
    priority: int = 0,
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
        pad_connection=pad_connection,
        priority=priority,
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
    Text on a back layer is mirrored (`justify mirror`), as KiCAD draws it:
    unmirrored back text fails DRC `nonmirrored_text_on_back_layer`.
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
    if layer.startswith("B."):
        node[-1].append([sym("justify"), sym("mirror")])
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
        # pcbnew writes -180 .. 180 (270 saves as -90); report 0 .. 360
        rot = (float(at[3]) if at and len(at) > 3 else 0.0) % 360 + 0.0
        x_mcp, y_mcp = file_to_pcb_xy(x_k, y_k)
        out.append(
            {
                "reference": ref or "?",
                "value": value or "",
                "lib_id": fp[1] if len(fp) > 1 and isinstance(fp[1], str) else "",
                "layer": layer,
                "position_mm": [round_mm(x_mcp), round_mm(y_mcp)],
                "rotation": rot,
                "courtyard_mm": footprint_courtyard_bbox(fp),
                "body_mm": footprint_layer_bbox(fp, ("F.Fab", "B.Fab")),
            }
        )
        side = connector_edge_side(fp)
        if side:
            out[-1]["edge_side"] = side
        crt = [p for shape in _local_shapes(fp, ("F.CrtYd", "B.CrtYd")) for p in shape]
        if len({round(p[0], 3) for p in crt}) > 2 or len({round(p[1], 3) for p in crt}) > 2:
            # e.g. a module's T-shaped antenna keep-out: parts may sit in the
            # notches that the bounding box covers.
            out[-1]["courtyard_is_box"] = False
    return out


def _local_shapes(fp: list, layers: tuple[str, ...]) -> list[list[tuple[float, float]]]:
    """Footprint-local outline points of each graphic on `layers`."""
    shapes = []
    for child in fp:
        if head_of(child) not in _CRTYD_SHAPES:
            continue
        layer = find_child(child, "layer")
        if not layer or str(layer[1]) not in layers:
            continue
        pts = [(float(n[1]), float(n[2])) for key in ("start", "mid", "end", "center")
               for n in [find_child(child, key)] if n and len(n) >= 3]
        poly = find_child(child, "pts")
        pts += [(float(xy[1]), float(xy[2])) for xy in (find_children(poly, "xy") if poly else [])]
        c, e = find_child(child, "center"), find_child(child, "end")
        if head_of(child) == "fp_circle" and c and e:
            cx, cy, ex, ey = float(c[1]), float(c[2]), float(e[1]), float(e[2])
            r = math.hypot(ex - cx, ey - cy)
            pts = [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]
        elif head_of(child) == "fp_rect" and len(pts) >= 2:
            (x1, y1), (x2, y2) = pts[0], pts[-1]
            pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        if pts:
            shapes.append(pts)
    return shapes


def _fp_frame(fp: list) -> tuple[float, float, float]:
    at = find_child(fp, "at")
    return (float(at[1]) if at and len(at) > 1 else 0.0,
            float(at[2]) if at and len(at) > 2 else 0.0,
            float(at[3]) if at and len(at) > 3 else 0.0)


def footprint_layer_bbox(fp: list, layers: tuple[str, ...]) -> list[float] | None:
    """Board box [x1, y1, x2, y2] of the footprint's graphics on `layers`
    (rotation applied); None when it has none there."""
    local = [p for shape in _local_shapes(fp, layers) for p in shape]
    if not local:
        return None
    frame = _fp_frame(fp)
    board = [file_to_pcb_xy(*_frame_point(x, y, frame)) for x, y in local]
    xs, ys = [p[0] for p in board], [p[1] for p in board]
    return [round_mm(min(xs)), round_mm(min(ys)), round_mm(max(xs)), round_mm(max(ys))]


def _board_side(fp: list, frm: tuple[float, float], to: tuple[float, float]) -> str:
    """Board side (left / right / top / bottom) a local vector points to."""
    frame = _fp_frame(fp)
    a, b = _frame_point(*frm, frame), _frame_point(*to, frame)
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "bottom" if dy > 0 else "top"


def connector_edge_side(fp: list) -> str | None:
    """Board side a connector's mating face points to: put the part on that
    board edge. From the footprint's "PCB Edge" line on Dwgs.User, else
    (connectors only) from the body sitting off its pads. None when unknown."""
    pads = [(float(at[1]), float(at[2])) for pad in find_children(fp, "pad")
            if (at := find_child(pad, "at")) and len(at) >= 3]
    if not pads:
        return None
    pc = (sum(p[0] for p in pads) / len(pads), sum(p[1] for p in pads) / len(pads))
    marker = None
    for child in fp:
        if head_of(child) in ("fp_text", "property") and len(child) > 2 \
                and "edge" in str(child[2] if head_of(child) == "fp_text" else "").lower():
            lay = find_child(child, "layer")
            at = find_child(child, "at")
            if lay and str(lay[1]) in ("Dwgs.User", "Cmts.User") and at:
                marker = (float(at[1]), float(at[2]))
    if marker is not None:
        lines = [shape for shape in _local_shapes(fp, ("Dwgs.User", "Cmts.User"))
                 if len(shape) == 2]
        if lines:
            def dist(seg):
                mx, my = (seg[0][0] + seg[1][0]) / 2, (seg[0][1] + seg[1][1]) / 2
                return math.hypot(mx - marker[0], my - marker[1])
            seg = min(lines, key=dist)
            mid = ((seg[0][0] + seg[1][0]) / 2, (seg[0][1] + seg[1][1]) / 2)
            horizontal = abs(seg[0][1] - seg[1][1]) < abs(seg[0][0] - seg[1][0])
            to = (pc[0], mid[1]) if horizontal else (mid[0], pc[1])
            if math.hypot(to[0] - pc[0], to[1] - pc[1]) > 1e-6:
                return _board_side(fp, pc, to)
    lib = str(fp[1]) if len(fp) > 1 and isinstance(fp[1], str) else ""
    if not lib.startswith("Connector") and not (get_footprint_reference(fp) or "").startswith("J"):
        return None
    body = [p for shape in _local_shapes(fp, ("F.Fab", "B.Fab")) for p in shape]
    if not body:
        return None
    x1, y1 = min(p[0] for p in body), min(p[1] for p in body)
    x2, y2 = max(p[0] for p in body), max(p[1] for p in body)
    bc = ((x1 + x2) / 2, (y1 + y2) / 2)
    off = (bc[0] - pc[0], bc[1] - pc[1])
    # The body overhangs its pads by more than a quarter of its size.
    if abs(off[0]) > 0.25 * (x2 - x1) and abs(off[0]) >= abs(off[1]):
        return _board_side(fp, pc, (bc[0], pc[1]))
    if abs(off[1]) > 0.25 * (y2 - y1):
        return _board_side(fp, pc, (pc[0], bc[1]))
    return None


_CRTYD_SHAPES = ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly")


def footprint_courtyard_bbox(fp: list) -> list[float] | None:
    """Board-coordinate box [x1, y1, x2, y2] around the footprint's courtyard
    (F.CrtYd / B.CrtYd), with its rotation applied; None when it has none.

    Graphics in a placed footprint are footprint-local. A circle counts with
    its full radius; an arc by its three points (a slight under-estimate on a
    wide arc).
    """
    return footprint_layer_bbox(fp, ("F.CrtYd", "B.CrtYd"))
