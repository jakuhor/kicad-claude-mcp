# Plan — connectivity model and the missing edit primitives

**Status: all four points implemented, 2026-09-18.** See "What was built" at
the end for what changed against this plan. Nothing from this plan is
outstanding; the live queue is `docs/open_points.md`.

Four pieces of work that close the gaps found in the capability survey of
2026-09-18. They are numbered 2–5 to match that survey; item 1 (issues 2 and 8
in `docs/kicad_mcp_issues.md`) is already done.

Build order and rationale are at the end. Points 3 and 5 are wrappers over
adapters that already work. Points 2 and 4 are new subsystems that share one
union-find implementation — build it in point 2, reuse it in point 4.

---

## Point 2 — Schematic connectivity

The server can build a schematic but cannot read one back. Nothing today
answers "what is connected to what", which is the question every design review
starts from.

**New module:** `src/kicad_claude/adapters/sch_netlist.py`. Read-only; it never
mutates the tree.

### Algorithm

Nets in a `.kicad_sch` are geometry, not declarations. There is no net list in
the file — it has to be derived. Four passes over a parsed sheet:

1. **Collect endpoints.** Wire ends from `(wire (pts (xy ..) (xy ..)))`, pin
   absolute positions, `(junction)`, `(label)`, `(global_label)`,
   `(hierarchical_label)`, `(no_connect)`. Pin positions need the
   symbol-instance transform that `sch_editor.list_pins_for_symbol` already
   implements — factor it out rather than duplicating it.

2. **Union-find over coincident endpoints.** Round coordinates to 4 decimals
   before hashing; KiCAD stores nanometre-exact values that will not compare
   equal as floats. Two wires that cross mid-segment are connected **only** if
   a `(junction)` sits at the crossing. A wire end that lands on another wire's
   middle without a junction is not a connection.

3. **Name each set.** Priority: local label, then global label, then a power
   symbol's `Value`, then a generated `Net-(R1-Pad1)`. Global labels merge sets
   across sheets. Hierarchical labels merge through `(sheet_pin)` by name.

4. **Expand buses.** `(bus_alias)` declarations and `DATA[0..7]` bracket
   notation both need resolving to their member nets.

### Tools (`src/kicad_claude/tools/schematic.py`)

| Tool | Returns |
|---|---|
| `list_sch_nets(scope="active"\|"all")` | `[{name, pin_count, pins: [{ref, pin}], sheets}]` |
| `trace_net(name)` | every pin, label and wire segment on the net |
| `get_pin_net(reference, pin)` | the net name at that pin |
| `find_dangling()` | wire ends, pins and labels touching nothing |

`find_dangling` is the one that pays off immediately: it catches the same class
of defect as ERC without a `kicad-cli` round trip.

### Tests

New file `tests/test_phase16_connectivity.py`.

- Two-resistor divider with a junction → 3 nets.
- The same sheet with the junction removed → the T-joint does not connect.
- A global label present on two sheets → one merged net.
- **Acceptance gate:** cross-check the whole net list against
  `kicad-cli sch export netlist --format kicadxml` on
  `tests/fixtures/kicad10_ecc83-pp_v2.kicad_sch`. Mark it `slow`.

### Risk

The coincidence rule in pass 2. If it is wrong, every net count is wrong and
the error is quiet. The kicadxml cross-check is the only trustworthy gate —
hand-written assertions on a toy fixture will agree with a broken
implementation. Write that test first.

### Effort

The largest of the four. Roughly 250 lines for the builder; the tools are thin.

---

## Point 3 — Symbol property tools

The adapter functions already exist and are `(property private ...)`-aware:
`sch_editor.get_symbol_property` and `sch_editor.set_symbol_property`. No MCP
tool exposes them, so changing a Value, Footprint or MPN still means editing
the `.kicad_sch` by hand.

### Tools (`src/kicad_claude/tools/schematic.py`)

| Tool | Notes |
|---|---|
| `set_symbol_property(reference, name, value, create=False)` | `create=True` appends a new `(property ...)`, copying `(at)` from `Value` and adding `(hide yes)` |
| `get_symbol_properties(reference)` | the whole dict |
| `set_dnp(reference, dnp)` | `(dnp yes\|no)` — a node, not a property |
| `set_in_bom(reference, value)`, `set_on_board(reference, value)` | same node family |

Each backs up before writing and returns the new value.

`set_symbol_property` must **refuse** `name="Reference"`. Renaming a reference
is `annotate_schematic`'s job; doing it here leaves the `(instances ...)` paths
pointing at the old designator.

### Tests

Extend `tests/test_phase3_schematic.py`. One slow test that sets DNP and
confirms the sheet still passes `kicad-cli sch erc`.

### Effort

Hours. The adapter is done.

---

## Point 4 — Ratsnest and unrouted connections

Depends on the union-find from point 2, applied to copper instead of wires.

**New module:** `src/kicad_claude/adapters/pcb_netlist.py`.

### Algorithm

- **Pad absolute positions.** Footprint `(at x y rot)` composed with each pad's
  own `(at)`, rotated by the footprint angle and mirrored for `B.Cu`.
  `pcb_editor` does not have this transform yet; it is the hard part of this
  point and is worth its own tested helper.
- **Copper graph.** `(segment)`, `(arc)`, `(via)` and filled
  `(zone (filled_polygon ...))`. Two pads are connected when a path of same-net
  copper joins them.
- **Zones.** KiCAD only stores `filled_polygon` after a fill. Treat a zone with
  no fill data as unfilled and say so in the result rather than assuming it
  connects. A stale fill is indistinguishable from a current one in the file,
  so the result should carry the caveat rather than hide it.

### Tools (`src/kicad_claude/tools/pcb.py`)

| Tool | Returns |
|---|---|
| `list_unrouted()` | `[{net, from: {ref, pad}, to: {ref, pad}, distance_mm}]`, longest first |
| `get_pad_position(reference, pad)` | `{x_mm, y_mm, layer, net}` |
| `net_route_status(net)` | `{pads, connected_groups, routed}` |

`get_pad_position` is worth having on its own. It is the primitive missing
behind the routine layout task "place each decoupling cap at its IC pin",
which currently cannot be expressed at all.

### Tests

New file `tests/test_phase17_pcb_connectivity.py`. A slow test that routes two
pads with `add_track` and asserts the pair disappears from `list_unrouted`.

### Risk

Zone connectivity. If it proves unreliable, ship copper-only connectivity with
an explicit `zones_ignored: true` in the result. A documented limitation is
useful; a silently wrong routed/unrouted answer is not.

### Effort

Around two days, most of it in the pad transform and its tests.

---

## Point 5 — Text and label editing

The last routine operations that still force hand edits of the `.kicad_sch`
(issue 6 in `docs/kicad_mcp_issues.md`, the remaining rows of its table).

### Adapter (`src/kicad_claude/adapters/sch_editor.py`)

`add_text`, `set_text`, `remove_text`, `rename_label`, `move_item`.

### Tools (`src/kicad_claude/tools/schematic.py`)

| Tool | Notes |
|---|---|
| `add_text(x_mm, y_mm, text, size_mm=1.27)` | no text tool exists today |
| `set_text(uuid, text)` / `remove_text(uuid)` | |
| `rename_label(old_name, new_name, scope="active"\|"all")` | must also rewrite matching `global_label`, `hierarchical_label` and `sheet_pin` |
| `move_item(uuid, x_mm, y_mm)` | wire, label, junction, no-connect; snaps to grid by default |

`rename_label` that misses a `sheet_pin` or a `global_label` on another sheet
splits one net into two, silently. Renaming across the whole hierarchy is the
default worth having, with `scope="active"` as the narrow case.

Identify items by `uuid`, not by coordinates. Coordinate matching is what makes
`remove_items_in_box` blunt, and a plan that repeats it inherits the same
problem.

### Tests

Extend `tests/test_phase3_schematic.py`. A hierarchy-wide `rename_label`
deserves a slow ERC check — that is where a missed `sheet_pin` shows up.

### Effort

About a day.

---

## Build order

| Order | Point | Effort | What it unblocks |
|---|---|---|---|
| 1 | 3 — symbol properties | hours | BOM fields, DNP, footprint assignment |
| 2 | 5 — text and labels | ~1 day | removes the last routine hand-edits |
| 3 | 2 — schematic connectivity | ~2–3 days | design review; prerequisite for point 4 |
| 4 | 4 — PCB connectivity | ~2 days | layout verification |

Points 3 and 5 come first because they are wrappers over working adapters and
each removes a documented manual workaround. Point 2 before point 4 because
point 4 reuses its union-find.

Every point adds its tests to the matching `tests/test_phase<N>_*.py`, and each
write path is proved with `kicad-cli` (`sch erc`, `pcb drc`) rather than by
inspection.


---

## What was built

All four points are implemented and tested. Three things turned out differently
from the plan.

### Point 2 — the T-joint rule was wrong in the plan

The plan said a wire ending on another wire's interior connects only when a
`(junction ...)` sits there. That is not what KiCAD does: an explicit junction
is needed where wires **cross** (an X), but a wire that **ends** on another
wire (a T) is connected on its own. The kicadxml gate caught this, which is
exactly why the plan put that gate first.

Result: `sch_netlist.build_sheet_nets` reproduces KiCAD's partition of
`tests/fixtures/kicad10_ecc83-pp_v2.kicad_sch` exactly — 13 nets, same pins on
each — and reproduces KiCAD's net names too, including the
`unconnected-(P8-Pad1)` form for a pin that reaches nothing.

Two naming details had to be matched: KiCAD writes `Pad<number>` when a pin's
name is empty, `~`, or merely repeats the pin number, and prefixes a net
`unconnected-` rather than `Net-` when only one pin sits on it.

Delivered: `list_sch_nets`, `trace_net`, `get_pin_net`, `find_dangling`.
`get_pin_net` returns a `connected` flag, because KiCAD names unconnected nets
too — the presence of a name says nothing.

### Point 4 — the gate was better than the plan's

The plan proposed a hand-built slow test that routes two pads. `kicad-cli pcb
drc --format json` reports `unconnected_items`, so KiCAD's own answer is
available for any board. The gate now runs against five demo boards covering
2- and 4-layer stacks, filled zones, thermal relief, inner-layer planes and
T-joined tracks, and matches on all five. A negative control cuts one net's
copper and requires both KiCAD and this module to notice — without it, a
checker that always answers "routed" would pass every positive case.

Four connection rules were needed that the plan did not anticipate, each found
by a gate mismatch:

1. **Thermal relief.** A relieved pad sits in a *hole* of the filled polygon,
   joined by narrow spokes, so a centre-in-polygon test reports it unconnected.
   Fixed by also accepting a polygon boundary within the pad's own radius.
2. **Vias into planes.** On a 4-layer board an SMD pad reaches the inner ground
   plane as pad → via → zone. Zones had to union with vias and track ends, not
   only with pads.
3. **Pad/track overlap.** A track running *past* a pad's edge connects it;
   requiring an endpoint inside the pad or the centre exactly on the line
   missed those. Now both are treated as bounding shapes — pad as a circle of
   its half-diagonal, track as a capsule of its width.
4. **T-joined tracks.** A PCB has no junction marker; a track ending mid-way
   along another is connected. Same rule as point 2's schematic T.

Zones are honoured only when filled. `list_unrouted` reports `zones_filled` and
`unfilled_zones` rather than guessing — the plan's fallback, made explicit in
every result instead of a one-off flag.

Delivered: `list_unrouted`, `get_pad_position`, `list_pads`,
`net_route_status`.

### Points 3 and 5 — as planned

`set_symbol_property` refuses `Reference` and `remove_symbol_property` refuses
KiCAD's five mandatory fields. `rename_label` defaults to `scope="all"`.
`move_item` drags a symbol's property fields with the symbol, which the plan
did not mention but is needed or the reference text stays behind.

Delivered: `get_symbol_properties`, `set_symbol_property`,
`remove_symbol_property`, `set_dnp`, `set_in_bom`, `set_on_board`, `add_text`,
`list_texts`, `set_text`, `remove_text`, `rename_label`, `move_item`.

### Verification

```
uv run pytest -q -m "not network"    373 passed, 9 skipped
uv run ruff check .                  47 errors — unchanged baseline, none new
```

New test files: `tests/test_phase16_connectivity.py` (26),
`tests/test_phase17_pcb_connectivity.py` (27). Points 3 and 5 added 41 tests to
`tests/test_phase3_schematic.py`, each with a `kicad-cli sch erc` round trip.
