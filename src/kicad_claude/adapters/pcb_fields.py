"""Footprint fields (Reference, Value, user fields) on a placed board.

File convention (board versions before KiCAD's 20260616 affine-transform
format, i.e. every KiCAD 10 board): a field's `(at x y angle)` holds the
position in the footprint's frame with the footprint rotation taken out, and
the text angle *absolute* on the board (pcb_io_kicad_sexpr.cpp, format of
PCB_TEXT; the parser's legacy branch rotates the position back by the
footprint orientation). `_frame_point` / `_unframe_point` are that rotation.

`tidy_fields` is the "keep references off copper, hide values" pass KiCAD
itself does not have: per footprint it tries spots around the courtyard and
takes the first whose text box clears pads, silkscreen graphics, the other
reference texts and the board edge.
"""

from __future__ import annotations

import math

from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import pcb_netlist
from kicad_claude.adapters.sch_io import find_child, find_children, head_of, property_name_index, sym
from kicad_claude.utils.geometry import round_mm

# Stroke-font glyph advance as a fraction of the text width (KiCAD newstroke,
# capitals and digits); a reference is mostly both.
_GLYPH_ADVANCE = 0.9
_SILK_SHAPES = ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly")
_GR_SILK_SHAPES = ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly")


def field_node(fp: list, name: str) -> list | None:
    for prop in find_children(fp, "property"):
        i = property_name_index(prop)
        if len(prop) > i + 1 and prop[i] == name:
            return prop
    return None


def _frame(fp: list) -> tuple[float, float, float]:
    at = find_child(fp, "at")
    return (float(at[1]) if at and len(at) > 1 else 0.0,
            float(at[2]) if at and len(at) > 2 else 0.0,
            float(at[3]) if at and len(at) > 3 else 0.0)


def _font(prop: list) -> tuple[list | None, list | None, list | None]:
    eff = find_child(prop, "effects")
    font = find_child(eff, "font") if eff else None
    size = find_child(font, "size") if font else None
    thick = find_child(font, "thickness") if font else None
    return eff, size, thick


def _justify(prop: list) -> set[str]:
    eff = find_child(prop, "effects")
    j = find_child(eff, "justify") if eff else None
    return {str(x) for x in j[1:]} if j else set()


def is_visible(prop: list) -> bool:
    h = find_child(prop, "hide")
    return not (h is not None and (len(h) < 2 or str(h[1]) == "yes"))


def text_box(text: str, anchor: tuple[float, float], angle: float, size: tuple[float, float],
             thickness: float, justify: set[str], mirrored: bool) -> list[float]:
    """Board box [x1, y1, x2, y2] of a single-line text (estimate)."""
    w = max(len(text), 1) * size[0] * _GLYPH_ADVANCE + thickness
    h = size[1] + thickness
    # offset of the box centre from the anchor, in the text's own frame
    dx = w / 2 if "left" in justify else -w / 2 if "right" in justify else 0.0
    if mirrored:
        dx = -dx
    dy = h / 2 if "top" in justify else -h / 2 if "bottom" in justify else 0.0
    vertical = round(angle) % 180 == 90
    if vertical:   # CCW on a Y-down board: text x runs up (90) or down (270)
        s = -1 if round(angle) % 360 == 90 else 1
        cx, cy = anchor[0] - s * dy, anchor[1] + s * dx
        w, h = h, w
    else:
        s = 1 if round(angle) % 360 == 0 else -1
        cx, cy = anchor[0] + s * dx, anchor[1] + s * dy
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


def field_info(fp: list, prop: list) -> dict:
    i = property_name_index(prop)
    at = find_child(prop, "at")
    lx = float(at[1]) if at and len(at) > 1 else 0.0
    ly = float(at[2]) if at and len(at) > 2 else 0.0
    angle = float(at[3]) if at and len(at) > 3 else 0.0
    bx, by = ed._frame_point(lx, ly, _frame(fp))
    layer = find_child(prop, "layer")
    layer_name = str(layer[1]) if layer and len(layer) > 1 else ""
    _, size, thick = _font(prop)
    sz = (float(size[1]), float(size[2])) if size and len(size) > 2 else (1.0, 1.0)
    th = float(thick[1]) if thick and len(thick) > 1 else 0.15
    text = str(prop[i + 1]) if len(prop) > i + 1 else ""
    return {"field": str(prop[i]), "text": text, "visible": is_visible(prop),
            "layer": layer_name, "position_mm": [round_mm(bx), round_mm(by)],
            "angle": angle % 360, "size_mm": list(sz), "thickness_mm": th,
            "box_mm": [round_mm(v) for v in text_box(text, (bx, by), angle, sz, th,
                                                      _justify(prop),
                                                      layer_name.startswith("B."))]}


def set_field(fp: list, name: str, *, visible: bool | None = None,
              position: tuple[float, float] | None = None, angle: float | None = None,
              size: float | None = None, layer: str | None = None,
              thickness: float | None = None) -> bool:
    """Edit one field in place. `position` is the board point, `angle` the
    absolute text angle; `size` scales the thickness with it unless
    `thickness` is given. Returns True when the tree changed."""
    prop = field_node(fp, name)
    if prop is None:
        raise KeyError(f"footprint {ed.get_footprint_reference(fp)!r} has no field {name!r}")
    before = repr(prop)
    if position is not None or angle is not None:
        at = find_child(prop, "at")
        if at is None:
            at = [sym("at"), 0, 0, 0]
            prop.insert(property_name_index(prop) + 2, at)
        while len(at) < 4:
            at.append(0)
        if position is not None:
            lx, ly = ed._unframe_point(float(position[0]), float(position[1]), _frame(fp))
            at[1], at[2] = round_mm(lx), round_mm(ly)
        if angle is not None:
            a = float(angle) % 360
            at[3] = int(a) if a.is_integer() else round_mm(a)
    if layer is not None:
        node = find_child(prop, "layer")
        if node is None:
            raise ValueError(f"field {name!r} has no layer node")
        node[1] = layer
    if visible is not None:
        h = find_child(prop, "hide")
        if visible and h is not None:
            prop.remove(h)
        elif not visible and h is None:
            node = find_child(prop, "layer")
            idx = prop.index(node) + 1 if node is not None else len(prop)
            prop.insert(idx, [sym("hide"), sym("yes")])
        elif not visible and len(h) > 1 and str(h[1]) != "yes":
            h[1] = sym("yes")
    if size is not None:
        _, sz, th = _font(prop)
        if sz is None:
            raise ValueError(f"field {name!r} has no font size")
        old = float(sz[1]) or 1.0
        sz[1] = sz[2] = round_mm(float(size))
        if th is not None and len(th) > 1 and thickness is None:
            th[1] = round_mm(float(th[1]) * float(size) / old)
    if thickness is not None:
        font, _, th = _font(prop)
        if font is None:
            raise ValueError(f"field {name!r} has no font")
        if th is None:
            font.append([sym("thickness"), round_mm(float(thickness))])
        else:
            th[1:] = [round_mm(float(thickness))]
    return repr(prop) != before


# --------------------------------------------------------------------------- #
# Placement pass
# --------------------------------------------------------------------------- #


def _overlap(a: list[float], b: list[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _grow(b: list[float], m: float) -> list[float]:
    return [b[0] - m, b[1] - m, b[2] + m, b[3] + m]


def _seg_boxes(points: list[tuple[float, float]], closed: bool, width: float) -> list[list[float]]:
    pts = points + ([points[0]] if closed and len(points) > 2 else [])
    out = []
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        out.append(_grow([min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)], width / 2))
    return out


def _shape_boxes(node: list, frame: tuple[float, float, float] | None) -> list[list[float]]:
    """Boxes along a silk graphic (segments as their own boxes, so a rect
    outline does not block its inside)."""
    def pt(n):
        x, y = float(n[1]), float(n[2])
        return ed._frame_point(x, y, frame) if frame else (x, y)
    stroke = find_child(node, "stroke")
    wn = find_child(stroke, "width") if stroke else find_child(node, "width")
    width = float(wn[1]) if wn and len(wn) > 1 else 0.12
    kind = (head_of(node) or "")[3:]
    s, e, c, m = (find_child(node, k) for k in ("start", "end", "center", "mid"))
    if kind == "line" and s and e:
        return _seg_boxes([pt(s), pt(e)], False, width)
    if kind == "rect" and s and e:
        (x1, y1), (x2, y2) = (float(s[1]), float(s[2])), (float(e[1]), float(e[2]))
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        corners = [ed._frame_point(x, y, frame) if frame else (x, y) for x, y in corners]
        return _seg_boxes(corners, True, width)
    if kind == "poly":
        poly = find_child(node, "pts")
        p = [pt(xy) for xy in (find_children(poly, "xy") if poly else [])]
        return _seg_boxes(p, True, width) if p else []
    if kind == "circle" and c and e:
        (cx, cy), (ex, ey) = pt(c), pt(e)
        r = math.hypot(ex - cx, ey - cy)
        return [_grow([cx - r, cy - r, cx + r, cy + r], width / 2)]
    if kind == "arc" and s and e:
        p = [pt(n) for n in (s, m, e) if n]
        xs, ys = [q[0] for q in p], [q[1] for q in p]
        return [_grow([min(xs), min(ys), max(xs), max(ys)], width / 2)]
    return []


def _side(layer: str) -> str:
    return "B" if layer.startswith("B.") else "F"


def _pad_on_side(pad: dict, side: str) -> bool:
    return any(lay.startswith(side + ".") or lay.startswith("*.") for lay in pad["layers"])


def _candidates(box: list[float], w: float, h: float, gap: float):
    """Text-box centres around `box`: above, below, right, left, then the
    corners, then the same one step further out."""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    for step in (0.0, 0.5, 1.0):
        g = gap + step
        yield cx, y1 - g - h / 2
        yield cx, y2 + g + h / 2
        yield x2 + g + w / 2, cy
        yield x1 - g - w / 2, cy
        yield x1 + w / 2, y1 - g - h / 2
        yield x2 - w / 2, y1 - g - h / 2
        yield x1 + w / 2, y2 + g + h / 2
        yield x2 - w / 2, y2 + g + h / 2


def tidy_fields(tree: list, references: list[str] | None = None, hide_values: bool = True,
                size: float | None = None, clearance: float = 0.2,
                hide_unplaced: bool = False, min_thickness: float = 0.0,
                min_height: float = 0.0) -> dict:
    """Move each chosen footprint's Reference (on silkscreen) to a free spot
    next to its courtyard, keep it horizontal, and hide Values that sit on a
    silkscreen layer. A Reference thinner than `min_thickness` or smaller
    than `min_height` (the board's text rules) is raised to them first.
    Mutates `tree`."""
    fps = list(ed.iter_footprints(tree))
    wanted = set(references) if references else None
    if wanted:
        missing = wanted - {ed.get_footprint_reference(fp) for fp in fps}
        if missing:
            raise KeyError(f"no footprint(s) {sorted(missing)}")

    obstacles: dict[str, list[list[float]]] = {"F": [], "B": []}
    pads = pcb_netlist.list_pads(tree)
    for pad in pads:
        xs = [p[0] for p in pad["corners"]]
        ys = [p[1] for p in pad["corners"]]
        box = _grow([min(xs), min(ys), max(xs), max(ys)], clearance)
        for side in ("F", "B"):
            if _pad_on_side(pad, side):
                obstacles[side].append(box)
    for fp in fps:
        frame = _frame(fp)
        for child in fp:
            if head_of(child) in _SILK_SHAPES:
                lay = find_child(child, "layer")
                if lay and str(lay[1]) in ("F.SilkS", "B.SilkS"):
                    obstacles[_side(str(lay[1]))] += [_grow(b, clearance)
                                                      for b in _shape_boxes(child, frame)]
    for child in tree[1:]:
        if head_of(child) in _GR_SILK_SHAPES:
            lay = find_child(child, "layer")
            if lay and str(lay[1]) in ("F.SilkS", "B.SilkS"):
                obstacles[_side(str(lay[1]))] += [_grow(b, clearance)
                                                  for b in _shape_boxes(child, None)]

    outline = ed.get_board_outline_polygon_kicad(tree)
    edge = None
    if outline:
        xs, ys = [p[0] for p in outline], [p[1] for p in outline]
        edge = [min(xs) + clearance, min(ys) + clearance, max(xs) - clearance, max(ys) - clearance]

    # texts that stay put block the ones being placed
    texts: dict[str, list[list[float]]] = {"F": [], "B": []}
    todo = []
    for fp in fps:
        ref = ed.get_footprint_reference(fp)
        mine = wanted is None or ref in wanted
        for prop in find_children(fp, "property"):
            info = field_info(fp, prop)
            on_silk = info["layer"] in ("F.SilkS", "B.SilkS")
            if not (on_silk and info["visible"]):
                continue
            if mine and info["field"] == "Reference":
                todo.append((fp, ref))
            elif mine and info["field"] == "Value" and hide_values:
                continue
            else:
                texts[_side(info["layer"])].append(_grow(info["box_mm"], clearance))

    moved, hidden, unplaced, values_hidden = [], [], [], []
    if hide_values:
        for fp in fps:
            ref = ed.get_footprint_reference(fp)
            prop = field_node(fp, "Value")
            if (wanted is None or ref in wanted) and prop is not None:
                info = field_info(fp, prop)
                if info["visible"] and info["layer"] in ("F.SilkS", "B.SilkS"):
                    set_field(fp, "Value", visible=False)
                    values_hidden.append(ref)

    raised = []
    for fp, ref in todo:
        if size is not None:
            set_field(fp, "Reference", size=size)
        prop = field_node(fp, "Reference")
        before = repr(_font(prop)[1:])
        _, sz, _th = _font(prop)
        if sz is not None and min_height and float(sz[2]) < min_height - 1e-9:
            set_field(fp, "Reference", size=min_height)
        _, _sz, th = _font(prop)
        if min_thickness and (th is None or float(th[1]) < min_thickness - 1e-9):
            set_field(fp, "Reference", thickness=min_thickness)
        if repr(_font(prop)[1:]) != before:
            raised.append(ref)
        info = field_info(fp, prop)
        side = _side(info["layer"])
        body = ed.footprint_courtyard_bbox(fp)
        if body is None:
            own = [p for p in pads if p["ref"] == ref]
            pts = [c for p in own for c in p["corners"]] or [_frame(fp)[:2]]
            body = [min(p[0] for p in pts), min(p[1] for p in pts),
                    max(p[0] for p in pts), max(p[1] for p in pts)]
        probe = text_box(info["text"], (0.0, 0.0), 0.0, tuple(info["size_mm"]),
                         info["thickness_mm"], set(), False)
        w, h = probe[2] - probe[0], probe[3] - probe[1]
        spot = None
        for cx, cy in _candidates(body, w, h, clearance):
            box = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
            if edge and not (box[0] >= edge[0] and box[1] >= edge[1]
                             and box[2] <= edge[2] and box[3] <= edge[3]):
                continue
            if any(_overlap(box, o) for o in obstacles[side]) or \
                    any(_overlap(box, t) for t in texts[side]):
                continue
            spot = (cx, cy, box)
            break
        if spot is None:
            if hide_unplaced:
                set_field(fp, "Reference", visible=False)
                hidden.append(ref)
            else:
                unplaced.append(ref)
                texts[side].append(_grow(info["box_mm"], clearance))
            continue
        cx, cy, box = spot
        # anchor = box centre corrected for the field's justification
        j = _justify(prop)
        dx = -w / 2 if "left" in j else w / 2 if "right" in j else 0.0
        if side == "B":
            dx = -dx
        dy = -h / 2 if "top" in j else h / 2 if "bottom" in j else 0.0
        set_field(fp, "Reference", position=(cx + dx, cy + dy), angle=0)
        texts[side].append(_grow(box, clearance))
        moved.append({"ref": ref, "position_mm": [round_mm(cx + dx), round_mm(cy + dy)]})
    return {"moved": moved, "values_hidden": values_hidden, "references_hidden": hidden,
            "unplaced": unplaced, "raised_to_minimum": raised}
