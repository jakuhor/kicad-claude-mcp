# Open points

The one live queue for this server. **Only open items live here.**

- A new field report or gap goes in as a new item at the end, with the next
  free number (numbers are never reused).
- When an item is done, delete it from this file and describe the finished
  capability in `README.md` (the capability, not the history — git keeps
  that).
- Mark decisions taken with the user *(decided, date)* so they are not
  re-asked.

The current epic is **R7 — one tool**: absorb what kicad-happy does that this
server does not, so the user runs one tool under their control *(decided,
2026-11-08)*. kicad-happy stays installed until parity; removing it is the
user's call (OP-8). Phase order *(decided, 2026-11-08)*: P-C and P-A done,
P-D (EMC, `analyze_emc`), P-E (`simulate_subcircuits`, incl. vendor op-amp
models), P-F (release tools, assembly review with 3D heights), P-B (BOM
diff, fab BOM + CPL, field write-back), the circuit detectors (P-C
remainder: power, analog, digital, misc) and the datasheet consumers
(`review_part_ratings`), the DC operating-point model (`dc_model`: switch
scenarios, pin clamps, zeners), their refinements and the remaining
detector groups (OP-9 … OP-12) done 2026-11-09.

---

## OP-8 — Retire kicad-happy (user decision)

OP-1 … OP-12 and OP-17 are done: the retire condition decided on
2026-11-11 is met (review checks, layout thermal + DFM, SPICE + datasheet
quality; inputs and workflow were not required). Remaining step, the
user's: uninstall the plugin (`/plugin` → uninstall `kicad-happy@kicad-happy`),
then delete this item.

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
