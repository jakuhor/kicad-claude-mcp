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
(`review_part_ratings`) done 2026-11-09.

---

## OP-7 — Limits of the netlist voltage model

`analyze_power_tree` / `review_connectivity` know a net's DC voltage only
from regulator outputs, series links, resistor dividers and caller
`rail_voltages`:
- nets behind switches (back-to-back FETs, load switches) and gate-drive
  nodes are unknown unless the caller passes them, so most FET VDS / VGS and
  TVS checks on such nets are skipped;
- the divider model ignores pin currents and internal clamps (LT8471 OV/UV
  clamps at 1.37 V; the review reports the open-circuit 1.63 V);
- a model of switch states (on / off) would let both cases be checked.

## OP-8 — Retire kicad-happy (user decision)

OP-1 … OP-6 are done. Compare against kicad-happy once more and let the user
decide whether to uninstall it. Not ported, as out of scope for this user's
boards *(decided 2026-11-09: the four detector groups power / analog /
digital / misc were chosen)*: Ethernet, HDMI / DVI, LVDS, memory, RF chains
and matching, BMS, battery chargers, motor drivers, addressable LEDs,
isolation barriers, thermocouple / RTD, wireless modules, energy
harvesting, transformer feedback, certification hints.

## OP-9 — Limits of the current model (`review_part_ratings`)

- Inductor ripple (and so the peak against Isat) is computed for bucks
  only; Cuk / Zeta / SEPIC / boost inductors are checked at their average.
- An internal stage of a regulator IC (LT8471 Skyhook output) is carried
  upstream only when its voltage is known (pass it in `rail_voltages`);
  inductors of such a stage are listed as not modelled.
- Efficiency is the caller's number (default stated as assumed), not a
  loss model; Tj uses the datasheet's rth_ja, not the layout.
- LED VF is the datasheet's value at its stated IF, not at the operating
  current.
