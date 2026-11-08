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
then P-D, P-E, P-F; P-B not scheduled.

---

## OP-1 — EMC rule set (R7 P-D)

kicad-happy's `emc` skill has 44 rules in 18 categories (ground planes,
decoupling, I/O filtering, switching harmonics, clock routing, diff-pair skew,
board-edge radiation, PDN impedance, return paths, crosstalk, ESD, shielding,
switching-inductor leakage) plus a pre-compliance test plan. The server has
`analyze_ground_coverage`, `find_long_traces`, `validate_decoupling_caps`,
`check_return_path_continuity`, `estimate_crosstalk`.

Port selectively. On Firbox kicad-happy's EMC output was mostly false errors,
which the port must not repeat:
- inner planes treated as signal layers;
- wrong switching frequency — use the regulator's `fsw` from the datasheet
  spec cache (and the RT resistor) instead of a default;
- wrong sign on the `-18V` rail — use `analyze_power_tree`'s
  `rail_voltages`.

## OP-2 — SPICE automation (R7 P-E)

Detect simulatable subcircuits from the netlist (RC / LC filters, dividers,
op-amp stages, crystal load caps), generate testbenches, run ngspice, report
pass / warn / fail against the design intent; Monte Carlo over tolerances;
optionally PCB parasitics. Today: `export_spice_netlist`,
`run_ngspice_simulation` only. Use manufacturer models where available and
say when a model is generic (Firbox convention).

## OP-3 — Gerber analysis, design diff, lifecycle (R7 P-F)

- Gerber / drill analyzer (`export_gerbers` only writes today).
- Design diff between two revisions (schematic and PCB).
- Lifecycle / obsolescence audit per MPN.
- What-if sweeps (component value changes through the review checks).
- PCB review gaps vs kicad-happy: assembly checks, schematic <-> PCB
  cross-verify beyond DRC parity, a fab release gate inside the server (the
  Firbox `fab-checklist` skill is project-side).

## OP-4 — Domain detectors (R7 P-C remainder, backlog)

kicad-happy has ~50 circuit-specific detectors (`domain_detectors.py`,
`validation_detectors.py`). Scope with the user before porting — candidates
for this user's boards: audio / op-amp stages, USB-PD, ESD audit, crystals /
clock distribution, LED circuits, reset supervisors. *(decided 2026-11-08:
backlog, revisit later)*

## OP-5 — BOM lifecycle (R7 P-B, not scheduled)

Bulk write-back of MPN / Manufacturer / distributor part numbers into symbol
fields, BOM diff between revisions, JLCPCB / PCBWay BOM + CPL in the fab's own
format with a rotation-offset table. Today: `set_symbol_property`,
`enrich_bom_with_sourcing` (CSV), `export_bom`, `export_pos`. *(decided
2026-11-08: not scheduled; kept so the parity gap stays visible)*

## OP-6 — Datasheet coverage and the checks that would consume it

The spec cache has no consumer yet for:
- inductor `Isat` / `Irms` — needs a per-rail current model (load budget);
- thermal: `rth_ja` x dissipation vs `tj_max` per part;
- capacitor voltage from the fitted MPN's datasheet (today `cap_voltage`
  uses the schematic `Voltage` field) and DC-bias derating;
- LED / constant-current regulator, common-mode choke, connectors.

On Firbox 8 of 20 MPNs are not extracted for that reason (150060RS75000,
NSI50010YT1G, ACM7060, MSY5050, XAL4020, YNR4030, HL-SMA-KE-02,
USB4105-GF-A).

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

When OP-1 … OP-3 are done (and OP-4 / OP-5 decided), compare against
kicad-happy once more and let the user decide whether to uninstall it.
