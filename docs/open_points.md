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

OP-1 … OP-12 are done. Compare against kicad-happy once more and let the
user decide whether to uninstall it.

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
