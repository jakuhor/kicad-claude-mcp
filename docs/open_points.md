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

OP-1 … OP-12 are done. Compared against kicad-happy 3.0.1 (2026-11-06) on
2026-11-11: not yet a full replacement; the gaps are OP-17. Retire it once
OP-17's review checks, layout thermal + DFM, and SPICE + datasheet quality
groups are closed; inputs + workflow are not required *(decided,
2026-11-11)*.

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

## OP-17 — kicad-happy 3.0.1 parity gaps

kicad-happy analyses; this server also edits, routes, exports and reads
connectivity from KiCAD's own netlist, ERC and DRC (so net naming, DNP,
per-instance references and pin alternates are KiCAD's by construction —
much of kicad-happy 3.0.1's fix list does not apply). What it has and this
server does not:
- Design-review checks: inrush, sleep-current / power budget, reverse-polarity
  FET, fuse sizing, CAN termination, I2C rise time (only the 3 mA sink is
  checked), UART voltage domains beyond DG-LEVEL, resistor power derating,
  derating profiles (commercial / military / automotive), over-designed parts,
  temperature-grade audit, alternative-part suggestions.
- Thermal from the layout: package Rθ_JA table (only datasheet values here),
  thermal-via correction and adequacy, MLCC / electrolytic proximity to hot
  parts (TP-001/002).
- PCB DFM score and assembly-complexity score.
- SPICE: LTspice and Xyce backends, model lookup / generation / caching from
  distributor specs, external-deck runner; EMC harmonic-limit screen.
- Datasheets: page selection and quality scoring, PDF SHA-256 bound to each
  extraction, quote verification against the page (Deep Review gate).
- Not required for retiring kicad-happy *(decided, 2026-11-11)*:
  inputs: KiCad 5–9 files (this server: KiCad 10 only), KiCad 10 design
  variants (`--variant`), PDF schematics / reference designs.
- Not required either *(decided, 2026-11-11)*: workflow — GitHub Action and PR comments, GitHub issue export, analysis run
  folders with provenance, report generation, OSHWA / standards checklists,
  JLCPCB / PCBWay ordering knowledge, per-supplier order files and a BOM
  tracking CSV.
- Validation: kicad-happy runs on a 5,800-project corpus; this server is
  validated on KiCAD's demo designs and synthetic netlists (OP-13).
