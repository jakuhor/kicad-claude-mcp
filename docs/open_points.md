# Open points

The one live queue for this server. **Only open items live here.**

- A new field report or gap goes in as a new item at the end, with the next
  free number (numbers are never reused).
- When an item is done, delete it from this file and describe the finished
  capability in `README.md` (the capability, not the history — git keeps
  that).
- Mark decisions taken with the user *(decided, date)* so they are not
  re-asked.

## OP-13 — Accepted limits of the estimators (no work planned)

Kept so they stay visible; each is stated in the tool output.
- The DC model is static: no gate-charge, inrush or soft-start transients.
- Efficiency curves come from the datasheet, often an application circuit at
  other VIN / VOUT / L (flagged "FAR from this stage"); capacitor ESR and
  switching losses outside the IC are not priced.
- Pin currents, drive limits and output levels exist only where the spec
  cache has them (DC-NODATA lists the gaps).
- The circuit detectors are pin-name heuristics, validated on KiCAD's demo
  designs and synthetic netlists; a new design style can still trip them.

---

Field test 2026-11-14: JH_LED_control rebuilt from `requirements.md`
(`source_repo/test_board_v2`). All its server items are done (README); the
board itself is left below.

## OP-28 — Finish the v2 board (acceptance, layout work)

The user does this layout; the server only supplies tools *(decided,
2026-11-16)*.

`source_repo/test_board_v2/hardware`, state 2026-11-14: ERC 0 / 0, DRC 0
errors, 0 parity, **12 unconnected** (`list_unrouted` agrees), 92 warnings
(86 silkscreen). Done: U5 re-placed with its antenna keep-out in place, solid
power pours, thermal vias in U1 EP and U5 EPAD, autoroute, part ratings with
`loads={"/LED_24V": 5, "+3V3": 0.4}` (1 warning: U2 Tj 210 °C at the assumed
250 °C/W; re-run with `vref={"U1": 1.0}, rth_ja={"U2": 60.3}`).
- Open links sit in the Q2 gate network (Q2_G, D8_A, SW_CTRL, LED_24V) and
  VIN / GND around U1. `autoroute_pcb(route_pour_nets=True)` on a copy
  (2026-11-16): DRC 12 -> 1 unconnected (GND, Q2_G), 0 errors; its
  `pour_guard` names C13.2 (LED_24V) and R18.1 (VIN) as beyond their pours.
- Still to do: GND stitching, silkscreen per requirements §9.4
  (`tidy_footprint_fields` on a copy: 89 -> 24 silk warnings; the rest are
  the 9 references it finds no room for in the same crowded Q2 / Q3 block,
  plus 3 U5 outline-to-edge), F2 temperature rerating.
- Checked: `Diode:SMAJ24A` naming its cathode `A1` does not
  mislead the reviews: TVS pin pairs take names from the datasheet pin table
  by number, and a TVS is never a power-tree link.

---

Field test 3, 2026-11-16: JH_LED_control rebuilt from `requirements.md`:
schematic, placement and pours, no routing
(`source_repo/test_board_v3/hardware`). All its server items (OP-37 …
OP-39) are done (README). Checked: KiCAD's DRC is right not to flag C11 /
C12 / R15 / C17 beside the ESP32-C6-MINI-1: its courtyard is T-shaped (the
antenna keep-out above the module), and they sit in the notches beside the
body that only the courtyard's bounding box covers (`list_footprints` now
says `courtyard_is_box: false`).

---

Field test 4, 2026-11-17: add a `Mouser` field (Mouser part number) to every
symbol of `~/github/JH_LED_control/hardware`, cost-optimise its BOM and swap
U1. All its server items (OP-40 … OP-44) are done (README).

---

IPC review, 2026-11-19 *(decided)*: no "IPC Class 3 verdict". Class 3 is a
fab acceptance class (IPC-6012 / IPC-A-600) ordered from the fab, not a
layout property; findings say "design margin for Class 3", never
"compliant". IPC values are cited by section and table, not copied in bulk.

## OP-45 — IPC-2221B voltage spacing per net pair

KiCAD's DRC does not know net voltages; this server does (rail voltages,
`analyze_power_tree`). For each pair of nets on one layer, take the voltage
difference, look up the IPC-2221B minimum conductor spacing for the
conductor category (B1 internal, B2 external uncoated, B3 external coated,
A6 / A7 assembly; altitude above 3050 m noted) and flag copper closer than
that. Inputs: `rail_voltages`, category per layer or a coated flag. Report
the cited table row. Fits beside `CERT-ISO` (which only names IEC 62368-1
creepage). New rule ID in the README table.

## OP-46 — IPC-D-356 and IPC-2581 export

Add both to `export(kind=…)` through `kicad-cli pcb export ipcd356` /
`ipc2581` (check the exact subcommands and options in KiCAD 10.0.8's
`kicad-cli pcb export --help`). D-356 is the netlist the fab uses for
electrical test; put it in `export_fab_package` too (option, default on).
`analyze_gerbers` may cross-check that the D-356 net count matches the board.

## OP-47 — IPC class presets (annular ring, holes)

Add presets to `src/kicad_claude/data/fab_presets.json` for IPC-2221 / 2222
producibility level B with Class 2 and Class 3 ring targets (e.g.
`ipc_class2_level_b`, `ipc_class3_level_b`): `min_via_annular_width`,
`min_via_diameter` / `min_via_drill`, `min_through_hole_diameter`,
`min_hole_to_hole`, `min_clearance`, with `source` naming the standard and
section. No code: `DFM-RING` / `DFM-DRILL` and DRC enforce them. Description
says "design margin", not acceptance. Values need the user's copy of the
standard to verify before they ship.
