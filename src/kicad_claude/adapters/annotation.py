"""Auto-annotate schematic reference designators (`R?` → `R1`, `R2`, …).

KiCAD keeps a reference per *sheet instance*: a symbol on a sheet file that
the hierarchy uses twice carries two `(instances (project … (path …
(reference …))))` entries, one per use, and the `Reference` property only
mirrors one of them. Annotation therefore works on occurrences — one per
(symbol, instance path) — never on the property alone:

1. Collect every occurrence of every sheet.
2. The high-water mark per prefix counts every occurrence's reference.
3. Occurrences whose reference ends with `?` get the next free number, in
   reading order per instance (top-down, then left-right). Units of one
   multi-unit part (same prefix, lib_id and value on the same instance) share
   one number, as KiCAD packs them: `U?` unit 1 and unit 2 become U1A / U1B.
4. Each changed path gets its reference, and the `Reference` property follows
   the symbol's first instance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io

_REF_PATTERN = re.compile(r"([A-Za-z#_]+)(\d+)$")


@dataclass
class Occurrence:
    symbol: list
    ref_node: list | None  # the path's (reference …) node; None: no instances block
    path: str               # instance path, "" without an instances block
    order: int              # discovery order of the path, for stable sorting

    @property
    def reference(self) -> str:
        if self.ref_node is not None and len(self.ref_node) >= 2:
            return str(self.ref_node[1])
        return ed.get_symbol_property(self.symbol, "Reference") or ""


def _paths(symbol: list, project: str | None) -> list[tuple[str, list | None]]:
    """(path, reference node) per instance of `symbol` in `project`."""
    instances = sch_io.find_child(symbol, "instances")
    if not instances:
        return []
    blocks = sch_io.find_children(instances, "project")
    mine = [b for b in blocks if project is None or (len(b) > 1 and b[1] == project)]
    out = []
    for block in mine or blocks:  # a renamed project: take what is there
        for path in sch_io.find_children(block, "path"):
            ref = sch_io.find_child(path, "reference")
            out.append((str(path[1]) if len(path) > 1 else "", ref))
    return out


def occurrences(trees: Iterable[list], project: str | None = None) -> list[Occurrence]:
    """Every (symbol, instance) of `trees`, in file and path order."""
    out: list[Occurrence] = []
    order: dict[str, int] = {}
    for tree in trees:
        for s in ed.iter_instance_symbols(tree):
            paths = _paths(s, project)
            if not paths:
                out.append(Occurrence(s, None, "", order.setdefault("", len(order))))
            for path, ref in paths:
                out.append(Occurrence(s, ref, path, order.setdefault(path, len(order))))
    return out


def existing_max_per_prefix(tree: list) -> dict[str, int]:
    """Highest number per prefix over every occurrence of `tree`."""
    return _high_water(occurrences([tree]))


def _high_water(occs: list[Occurrence]) -> dict[str, int]:
    out: dict[str, int] = {}
    for o in occs:
        m = _REF_PATTERN.fullmatch(o.reference)
        if m:
            out[m.group(1)] = max(out.get(m.group(1), 0), int(m.group(2)))
    return out


def _position(s: list) -> tuple[float, float]:
    at = sch_io.find_child(s, "at")
    if not at or len(at) < 3:
        return (0.0, 0.0)
    return (float(at[1]), float(at[2]))


def _unit(s: list) -> int:
    u = sch_io.find_child(s, "unit")
    return int(u[1]) if u and len(u) > 1 else 1


def annotate(occs: list[Occurrence], *, counters: dict[str, int] | None = None,
             sort_by_position: bool = True) -> list[dict]:
    """Number every `?` occurrence; mutates the trees. Returns `{old, new, path}`."""
    if counters is None:
        counters = _high_water(occs)
    targets = [o for o in occs if o.reference.endswith("?") and o.reference[:-1]]
    if sort_by_position:
        # KiCAD Y grows downwards: smaller Y is higher on the page.
        targets.sort(key=lambda o: (o.order, _position(o.symbol)[1], _position(o.symbol)[0]))

    # (prefix, lib_id, value, path) -> [(number, units taken)]
    packages: dict[tuple, list[tuple[int, set[int]]]] = {}
    assignments: list[dict] = []
    for o in targets:
        old = o.reference
        prefix = old[:-1]
        key = (prefix, str((sch_io.find_child(o.symbol, "lib_id") or ["", ""])[1]),
               ed.get_symbol_property(o.symbol, "Value") or "", o.path)
        unit = _unit(o.symbol)
        number = next((n for n, units in packages.get(key, []) if unit not in units), None)
        if number is None:
            counters[prefix] = counters.get(prefix, 0) + 1
            number = counters[prefix]
            packages.setdefault(key, []).append((number, set()))
        for n, units in packages[key]:
            if n == number:
                units.add(unit)
        new = f"{prefix}{number}"
        if o.ref_node is not None:
            o.ref_node[1] = new
        else:
            ed.set_symbol_property(o.symbol, "Reference", new)
        assignments.append({"old": old, "new": new, "unit": unit,
                            **({"path": o.path} if o.path else {})})

    # The property mirrors the first instance of each symbol.
    changed = {id(o.symbol) for o in targets}
    seen: set[int] = set()
    for o in occs:
        if id(o.symbol) in seen or id(o.symbol) not in changed or o.ref_node is None:
            continue
        seen.add(id(o.symbol))
        if o.reference != ed.get_symbol_property(o.symbol, "Reference"):
            ed.set_symbol_property(o.symbol, "Reference", o.reference)
    return assignments


def annotate_tree(tree: list, *, counters: dict[str, int] | None = None,
                  sort_by_position: bool = True) -> list[dict]:
    """Annotate one sheet in place (every instance it has)."""
    return annotate(occurrences([tree]), counters=counters, sort_by_position=sort_by_position)


def annotate_sheets(sheet_paths: Iterable, sort_by_position: bool = True,
                    project: str | None = None) -> dict:
    """Annotate the whole hierarchy with one counter, and save changed sheets.

    All sheets are written as one edit (`safe_write.save_trees`): every lock
    is checked first, and a failure on one sheet restores the others, so the
    hierarchy is never left half-annotated.
    """
    from kicad_claude.adapters import safe_write

    paths = [Path(p) for p in dict.fromkeys(Path(p).resolve() for p in sheet_paths)]
    trees = {p: sch_io.parse_file(p) for p in paths}
    occs_by_sheet = {p: occurrences([trees[p]], project) for p in paths}
    all_occs = [o for p in paths for o in occs_by_sheet[p]]
    counters = _high_water(all_occs)

    per_sheet = {p: annotate(occs_by_sheet[p], counters=counters,
                             sort_by_position=sort_by_position) for p in paths}
    changed = [p for p in paths if per_sheet[p]]
    backups = dict(zip(changed, safe_write.save_trees([(p, trees[p]) for p in changed])))
    sheet_results = [{"sheet": str(p), "assignments": per_sheet[p],
                      "count": len(per_sheet[p]),
                      "backup": str(backups[p]) if backups.get(p) else None} for p in paths]
    return {"total_assignments": sum(len(a) for a in per_sheet.values()),
            "sheets": sheet_results, "high_water": counters}
