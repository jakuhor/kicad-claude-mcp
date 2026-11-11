# kicad-claude-mcp

> Design KiCAD PCBs from natural language. An MCP server that gives Claude
> Code (or any MCP client) **156 tools** to drive a complete PCB workflow —
> schematic capture, library management, PCB layout, autorouting, design
> review against the parts' datasheets, sourcing, manufacturing outputs,
> signal-integrity calculations, RF design, and more.

**156 MCP tools · 709 tests (642 fast, 61 acceptance, 6 network) · headless from idea to gerbers**

The server edits `.kicad_sch` / `.kicad_pcb` / `.kicad_pro` files directly, in
KiCAD 10's own formatting — one added wire is a ten-line git diff, not a
reformat of the whole sheet. You drive it via Claude Code; you open KiCAD only
to review the result.

---

## What you can build with this

```
> Create a project at /tmp/blinky for a USB-C powered ESP32 board.
> Index KiCAD's libraries, find ESP32-S3-WROOM-1 and a 3V3 LDO.
> Lay out the schematic with USB-C input, the LDO, the ESP32, and a status LED.
> Run ERC, fix any issues you find.
> 
> Place the components on a 50×30 mm 4-layer board with USB on the left edge,
> add a GND plane on B.Cu, route everything with Freerouting, and add length
> tuning to the USB diff pair so D+ and D- match within 0.5 mm.
> 
> Apply the JLCPCB design rules, run DRC, and export the fab package
> (gerbers + drill + pos + BOM with live DigiKey/Mouser stock).
```

Claude Code translates this into ~30 tool calls, each editing the project
files. You verify the result by opening KiCAD.

---

## Quick start

### Requirements

- Python 3.10+ and [`uv`](https://docs.astral.sh/uv/)
- KiCAD 10.x (with `kicad-cli` installed) — files are written in the formats KiCAD 10.0.6 writes; older projects are read
- Java 21+ (for autorouting via Freerouting; Java 24 works, Java 25 needed for the very latest Freerouting)
- *Optional*: `ngspice` (for SPICE simulation); DigiKey / Mouser / Farnell / TME API keys (live sourcing and datasheet lookup — the JLCPCB source needs no key)

### Install

```bash
git clone https://github.com/Greg3001/kicad-claude-mcp.git
cd kicad-claude-mcp
uv sync
cp .env.example .env       # fill API keys when ready (optional, only Phase 4+)
```

Download Freerouting 2.1.0 to `third_party/freerouting.jar` (only needed for `autoroute_pcb`):

```bash
mkdir -p third_party
curl -L https://github.com/freerouting/freerouting/releases/download/v2.1.0/freerouting-2.1.0.jar \
  -o third_party/freerouting.jar
```

### Install from a local clone on a branch

To run a branch — your own work, a fix you are testing, `dev` instead of
`main` — install from a local checkout of it. The server always runs from a
working tree: `server.py` sits at the repo root and is not shipped in the
wheel, so `uv pip install git+…` gives you the library without an entry point.

```bash
cd kicad-claude-mcp        # the clone from the previous step
git switch dev             # or: git switch -c my-feature
uv sync                    # re-run after every switch — dependencies may differ
```

Then point Claude Code at *that* directory (see below): `cwd` is the checkout
and `command` is its own `.venv` interpreter.

To keep several branches installed side by side, give each one its own working
tree and its own `.venv`:

```bash
git worktree add ../kicad-claude-mcp-dev dev
cd ../kicad-claude-mcp-dev
uv sync
```

Register each as a separate MCP server under its own name (`kicad`,
`kicad-dev`, …) so you can pick one per session. Two servers must not edit the
same project at once — each holds its own active-project state.

Switching a branch under a running server does not reload it: run `uv sync`,
then restart Claude Code.

### Wire into Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "kicad": {
      "command": "/ABSOLUTE/PATH/TO/kicad-claude-mcp/.venv/bin/python",
      "args": ["server.py"],
      "cwd": "/ABSOLUTE/PATH/TO/kicad-claude-mcp"
    },
    "kicad-dev": {
      "command": "/ABSOLUTE/PATH/TO/kicad-claude-mcp-dev/.venv/bin/python",
      "args": ["server.py"],
      "cwd": "/ABSOLUTE/PATH/TO/kicad-claude-mcp-dev"
    }
  }
}
```

> The `command` MUST be the absolute path to `.venv/bin/python` — the system
> Python doesn't have the dependencies. On Windows that path is
> `.venv\Scripts\python.exe`. Reload Claude Code, run `/mcp` to confirm
> `kicad` is connected, then ask for `Use the kicad ping tool`.

The second entry is optional — it is the branch checkout from the previous
section, exposed under its own name. Drop it if you only run one.

### First run

```
> Index KiCAD's libraries  (one-time, ~60 s)
> Create a project at /tmp/test for a voltage divider
> ... etc
```

---

## Capabilities

### Safe file writes

- **Round-trip formatting**: a write touches only the nodes it changed. The layout comes from a port of KiCAD's own `Prettify`; all 91 KiCAD 10 demo schematics round-trip byte-identically, so a git diff shows only the edit.
- **Every mutating tool goes through one writer**: it refuses while KiCAD holds the project's `.lck`, backs up to `<project>/.backups/<timestamp>_<file>` (newest 10 kept), re-parses the result and restores the backup if it does not parse. Control characters are escaped on write.
- **Stale locks**: the refusal says who holds the lock (KiCAD's lock file has only user and host) and whether a KiCAD process is running; `clear_stale_lock` removes it only on explicit request.
- **Strict arguments**: an unknown keyword is an error naming the closest accepted spelling, not silently ignored. Older spellings (`net_name` → `net`, …) still work and the response lists them under `deprecated_arguments`.

### Schematic capture

- **Symbol placement & connections**: `add_symbol` (with `footprint=` and arbitrary `fields=` in the same call), `add_wire`, `add_label`, `add_power_symbol`, `add_no_connect`, `move_symbol`, `remove_symbol`, `replace_symbol`, `get_pin_position`, `list_pins`
- **Derived library symbols** (`extends`, e.g. `TLV75733PDBV`, `2N7002`) are flattened like KiCAD's `LIB_SYMBOL::Flatten`, so their pins are placed and wired correctly.
- **Pins landing mid-wire**: KiCAD does not connect a pin to a wire's interior without a junction. `add_symbol` / `add_power_symbol` add the junction and report it (`junctions_added`).
- **Power ports**: any net name works (`add_power_symbol(net="GND_USB")`, built on a stock base symbol with the Value overridden, `base_symbol=` to choose it); the `#PWR` / `#FLG` reference is hidden as in KiCAD. `PWR_FLAG` never names a net.
- **Fields**: `move_symbol` carries the fields with the symbol (rotation included); `set_field_position`, `set_symbol_property`, `remove_symbol_property`, `get_symbol_properties`, `set_dnp`, `set_in_bom`, `set_on_board`
- **Editing and deletion**: `remove_wire`, `add_junction`, `remove_junction`, `remove_no_connect`, `move_item`, `rename_label`, `add_text` / `set_text` / `remove_text`, `remove_items_in_box` (both endpoints must be inside, so a crossing wire survives), and `remove_symbol(remove_connected_wires=True)` to drop the stubs left on the removed pins
- **Connectivity model**: `list_sch_nets`, `trace_net`, `get_pin_net`, `find_dangling` — the same connectivity KiCAD uses (net names from power symbols with a `power_in` pin, labels, hierarchy)
- **Grid discipline**: every placement snaps to KiCAD's 1.27 mm grid unless you pass `snap_to_grid=False`, so endpoints actually connect
- **Hierarchical sheets**: `add_sheet`, `set_active_sheet` (the root sheet by its file name too), `get_active_sheet`, `add_hierarchical_label`, `add_sheet_pin`, `list_sheets`; `list_components(sheet=…)` lists one sheet without switching
- **Buses**: `add_bus`, `add_bus_entry`, `add_bus_alias` — visual grouping of address/data lines
- **Annotation**: `annotate_schematic` — auto-numbers `R?` → `R1, R2, ...` across all sheets
- **Custom symbols**: `create_symbol` with full pin specifications and arbitrary fields; `refresh_lib_symbols` updates a sheet's cached library symbols

### Library management

- **KiCAD's libraries indexed** — `index_libraries` (project tables and KiCAD's global `sym-lib-table` / `fp-lib-table`, with `${VAR}` expansion), `search_symbol`, `search_footprint`, `get_symbol_details`. Search ranks exact name, then prefix, then substring, then fuzzy matches.
- **External parts**: `find_or_fetch_symbol`, `import_vendor_zip` (SnapEDA / Ultra Librarian), `list_vendor_parts`
- **Custom footprints**: `create_footprint` with auto-courtyard and silk outline

### Sourcing and datasheets

- **Availability**: `check_availability` against Mouser, DigiKey, TME, Farnell and JLCPCB (opt-in source: LCSC number, stock, USD price breaks, basic / preferred / extended library type, no key needed)
- **Single datasheet**: `fetch_datasheet` tries the given URL and each source's datasheet link in order and keeps the first body that really is a PDF (bot-protection HTML is rejected; TLS verification is never disabled); every attempt is reported
- **Project datasheet folder**: `sync_datasheets` gives every schematic MPN its PDF in `datasheets/` and records the mapping in `datasheets/manifest.kicad-claude.json`. Existing vendor-named files (`8471fd.pdf`) are matched through the symbol's Datasheet URL, the PDF's own text (MPN or family stem) or the file name; the rest is downloaded. The folder is `<project>/datasheets`, or the nearest parent `datasheets/` up to the git root (one shared folder for a multi-board repo). Reports unmapped parts and orphan PDFs.
- **Structured specs, extracted by the calling agent**: `get_datasheet_extraction_task` hands over the PDF path, the text of the pages holding the tables, the part's schematic pins and nets, and the schema; `save_datasheet_extraction` validates the record and caches it in `datasheets/specs/<MPN>.json`. Every value carries its PDF page and SI unit; the server checks each number against the cited page's text and warns when it is not there. A record goes `stale` when its PDF changes. `get_datasheet_specs` returns one record or the coverage. No value is taken from general knowledge. Pin behaviour the DC model uses goes in `pin_models` (clamp, trip current, bias current, input resistance, keyed by pin name). Graphs go in `curves` (points read off the plot, with page; e.g. a capacitor's `C_bias`, capacitance vs DC bias). The schema names the keys the analyzers read: capacitor `C`, `ESR`, `ESL`, rated `V`, `C_bias`; inductor `L`, `DCR`, `Isat`, `Irms*`; common-mode choke `Z_cm`, `DCR`, `I_rated`; LED `VF`, `IF`; constant-current regulator `I_reg`, `V_min`; connector `I_rated` (per contact) / `I_rated_total` (a rail's contacts together); `thermal.rth_ja` / `tj_max`; op-amp `GBW`; crystal `CL`.
- **BOM enrichment**: `enrich_bom_with_sourcing` queries the chosen sources per unique value and appends MPN / stock / price / URL columns (JLCPCB adds `jlc_lcsc`, `jlc_type`)
- API keys never reach the log: request URLs are not logged and key-like query values are redacted.

### PCB layout

- **2 to 32 copper layers** with auto-generated stackup (`set_layer_count`)
- **Footprint placement**: `add_footprint`, `move_footprint`, `place_footprints_grid`
- **Routing**: `add_track`, `add_via`, `add_via_array`, `add_meander` (length tuning)
- **Copper pours**: `add_zone`, `add_ground_plane` (auto-extracts board outline, rounded corners included)
- **Mechanical**: `add_mounting_hole` (M2/M2.5/M3/M4/M5), `add_fiducial`, `add_silk_text`
- **RF specials**: `add_rf_microstrip` (target Z₀), `add_ground_stitching`
- **Inspection**: `list_footprints`, `list_pads`, `get_pad_position`, `list_nets`, `list_unrouted` (with `summary` / per-net roll-up), `net_route_status`, `compute_trace_length`, `list_diff_pair_candidates`, `validate_diff_pair_length_match`
- **Rotation**: rotating a footprint rotates its pads too (pad angles in `.kicad_pcb` are absolute)
- **Sync**: `update_pcb_from_schematic` adds, updates and removes footprints and applies pad nets through KiCAD's own `pcbnew` from a kicadsexpr netlist (escaped net names such as `{slash}` survive); it saves the board only when something changed, `scope="fields"` updates only fields (fast), and the timeout scales with the design
- **Coordinates**: schematic and PCB tools both take KiCAD-native coordinates — mm, Y down, the numbers the GUI shows

### Autorouting

- **Freerouting integration** — `autoroute_pcb` runs the full pipeline (DSN export → routing → SES import) with timeout, parses stats from output
- **Diff pair coupling** — auto-detected by net naming (`_P/_N`, `+/-`, `DP/DM`); routed coupled when net class has `diff_pair_width`/`gap` set

### Validation

- **ERC and DRC** — `run_erc`, `run_drc` shell out to `kicad-cli` and parse the JSON (every sheet's violations); `run_drc(summary=True)` returns counts by type and severity, `max_violations=N` caps the lists
- **Custom DRC rules** — `add_drc_rule` writes `.kicad_dru` with constraint types (clearance, track_width, length, skew, diff_pair_gap, …) and conditions in KiCAD's expression language; `list_drc_rules`, `remove_drc_rule`, `clear_drc_rules`
- **Net classes + fab presets** — `apply_fab_preset` with `jlcpcb_2l_default`, `jlcpcb_2l_advanced`, `jlcpcb_4l`, `aisler_2l`, `aisler_4l`, `pcbway_default`, `oshpark_4l`, `permissive_prototype` (`list_fab_presets`). When the preset conflicts with the project's own rules it changes nothing and returns the conflicts with `decision_required`; the caller picks `mode="project"`, `"preset"` or `"stricter"` (per-rule stricter value wins); `dry_run=True` previews. `add_net_class`, `add_diff_pair_class`, `assign_net_class`

### Design review (from KiCAD's own netlist and the parts' datasheets)

Both tools export the whole hierarchy with `kicad-cli sch export netlist`, so the connectivity is exactly KiCAD's. They are pin-name and pin-type heuristics: every finding says what it saw, and should be confirmed against the schematic and datasheet.

- **`analyze_power_tree`** — finds each regulator (one record per channel on multi-channel parts), its topology, input and output rail, EN state (tied to input, divider, driven by another part's pin = sequencing, floating …), power-good pull-up, feedback divider (values, ratio), SET resistor, switch-node inductor and input / output capacitors. Builds the rail tree through series L / ferrite / 0 Ω links (DNP marked). Computes each Vout from the datasheet specs — FB divider with the right-polarity Vref, ANY-OUT pin strapping, or SET current × R — or from a caller `vref`, and carries rail voltages through the links. With those voltages it checks VIN against absolute maximum and recommended range, VOUT range, LDO headroom against dropout, and the EN pin voltage (divider / pull-up, power-good released) against the EN rating and turn-on threshold. Unregulated inputs (a USB-PD contract) come from `rail_voltages={"/VBUS": 20}`. Nets off the rails (EN dividers, multi-branch networks) come from the DC operating-point model below, so an EN pin held by an internal clamp is checked at the clamped voltage; solved nets are returned as `net_voltages`.
- **`review_connectivity`** — checks (all on by default, choose with `checks=`): IC power pins and how each rail is sourced; decoupling per IC supply net; open-drain outputs without pull-up; I2C buses (pull-ups, address strapping, fixed datasheet addresses); symbol pins against the datasheet pin table; every pin and pin pair (VDS, VGS, TVS standoff) against its absolute maximum at the net's voltage; capacitor DC voltage against its rating — the fitted MPN's extracted datasheet when there is one, else the `Voltage` field (`cap_derating`, default 0.8). The pin and capacitor checks run on the DC operating-point model in each switch scenario (`switch_states`: `auto` by default — every combination up to `max_switch_combos`, else all-on / all-off —, `all`, `both`, `on`, `off`, `none`; `switch_overrides={"Q3": "off"}`).
- **DC operating-point model** (`analyze_power_tree`, `review_connectivity`) — on top of the rail voltages: every fitted resistor network solved by nodal analysis (0 Ω, inductors and ferrites are shorts); datasheet pin models (`pin_models` in the spec record: internal clamp, trip current such as an OVLO threshold, bias current, input resistance; `regulator.en_pullup_current` is applied to EN pins); zeners that clamp (VZ max from the spec cache, else the part's value), solved iteratively; transistor switches — `on` joins drain and source and the gate driver (an IC output, directly or through a series resistor) pulls to the on level, limited to the pin's datasheet drive current (`pin_models` `i_sink` / `i_source`; above it the pin becomes a current source and DC-DRIVE warns); `off` passes only the body diode (VSD from the spec cache, else no drop) and leaves the far side at 0 V. A driver's high level is its `v_oh`, else its internal regulator's `v_out` (e.g. VDDD), else an unambiguous supply. `review_connectivity` runs every on / off combination of the switches up to `max_switch_combos` (default 16) (above that: all on, all off and each switch flipped alone) and reports each pin's worst case (`pin_abs_max_worst`, `cap_voltage_worst`); DC-NODATA lists what the model ran without (drive ratings, body-diode VSD, P / N type, zener VZ); findings that occur only in some states say which (`[when Q1 on]`). Static DC: capacitors open, no switching transients.
- **`review_part_ratings`** — parts against their datasheet ratings with a per-rail current model. `loads={"+5VD": 0.5}` (amps per rail) is carried up the power tree: series links unchanged, linear regulators Iin = Iout, switchers by power with `efficiency` per regulator (others use `default_efficiency`, reported as assumed), and from each root rail through the series path (inductors, common-mode choke winding, ≤ 1 Ω resistors, FET dies, series diodes) to the connector that brings power in. Inductor ripple from L, `fsw` (caller or datasheet), Vin, Vout for buck, boost, inverting buck-boost and two-inductor Ćuk / SEPIC / Zeta stages (ideal CCM duty; the switch peak is the sum of the inductor peaks, checked against the switch current limit), with a DCM flag and the DCM peak √(2·Iavg·ΔI) for single-inductor stages; two-inductor stages use the summed current (L1 ∥ L2): in DCM each winding's ripple shrinks with the on-time and the switch peak is √(2·ΣI·ΣΔI); a coupled inductor (one 4-pin part, windings 1-2 / 3-4) has half the ripple of two separate ones; a forced-PWM part (`regulator.light_load: fccm`) stays CCM. Efficiency comes from the caller, else the datasheet's `curves.efficiency*` (closest VIN / VOUT), else the default. A channel whose power enters through another pin (`regulator.channels.N.input_pin`, LT8471 C2) and pins driven at another pin + offset (`regulator.internal_outputs`, LT8471 SHOUT = C2 + Skyhook boost) come from the spec cache. Switcher Tj uses the converter loss minus what the spec cache can price outside the IC (inductor DCR at RMS current, rectifier VF × average current); parts without DCR / VF stay charged to the IC and are named. An efficiency curve far from the stage's VIN / VOUT is flagged as an estimate. Junction temperature Ta + rth_ja·P (`ambient_c`, default 40 °C, stated as assumed) for regulators (channels of one IC summed), FETs (I²·RDS(on) per die), series diodes and LED current regulators; rth_ja is the datasheet's test board. LED strings: current from the series R and VF (VF at the operating current from `curves.VF` when extracted), or a CCR's `I_reg` and its headroom.
- **`review_circuits`** — circuit detectors in eight groups (`groups=`), checked against eleven of KiCAD's bundled demo designs (no errors; acceptance test): **power** — USB Type-C CC termination (Rd 5.1 k per CC pin, Rp value vs its rail, controller pin, CC1 + CC2 on one resistor), D+ / D- of both plug orientations joined, clamps on connector supply nets, ground pins on power connectors, ESD clamps on external connector signal nets (`internal_connectors` excludes board-to-board and test connectors), enable loops and self-enable, reset nets, FB divider impedance and FB bias-current error; **analog** — op-amp inputs with no DC path, input coupling high-pass corner vs `audio_f_low_hz`, output coupling caps to a connector without a bleed resistor, unused op-amp channels, converter reference / VCOM caps, analog supply decoupling, split converter grounds with no join, ADC inputs without RC, jacks driven straight from an output, level shifters; **digital** — inputs with nothing driving them (pins the datasheet lets float — ANY-OUT, "may be left open" — are skipped; a symbol input the datasheet calls an output is `DG-PINTYPE`), boot / mode straps, I2C pull-ups stronger than the 3 mA sink, SPI chip selects shared or without pull-up, driver vs receiver supply voltage, branched clocks without a series resistor, USB data ESD, SWD pins brought out; **misc** — LEDs without a current limit, LED-driver set pins, display charge-pump / VCOMH caps, RTC backup pin, buttons without pull-up, switch matrices without diodes; **interfaces** — Ethernet magnetics, Bob Smith termination and centre taps, HDMI / DVI ESD, DDC pull-ups, HPD and +5V protection, LVDS receiver termination, flash WP / HOLD and DDR VREF, radio-module antenna path; **power_conversion** — charger set resistor and thermistor, BMS cell-sense RC and balancing, cell-protection sense, opto + shunt-reference feedback (bias, compensation), series supercap balancing, isolator grounds bridged, Y-caps, per-side decoupling; **drives** — motor-driver bootstrap, sense resistor and VM caps, freewheel diodes on low-side switches with inductive loads, thermocouple filtering, RTD reference resistor and its tolerance, addressable LED chains (decoupling, data level vs 0.7 VDD, series resistor); **rf** — RF pins reaching a connector without a DC block, matching networks (pi / T / L, all-DNP pads) and the standards the detected circuits point to (CISPR 32 / FCC 15B, RED / FCC 15C, IEEE 802.3 isolation, IEC 62133 / UN 38.3, IEC 62368-1, USB-IF, isolator and drive standards).

| Rule | Severity | Meaning |
|---|---|---|
| `PT-EN-FLOAT` / `PT-EN-GND` | error | Regulator EN unconnected / tied to ground |
| `PT-FB-NODIV`, `PT-FB-VAL` | warning | FB has no divider and is not tied to the output / divider value not parseable |
| `PT-VOUT-VREF`, `PT-VREF-POL`, `PT-SET` | info | Vout not computed: no Vref, no Vref of the needed polarity, no SET current |
| `PT-PG-PULLUP` | warning | A used power-good net has no pull-up |
| `PT-CIN` / `PT-COUT` | warning | No capacitor to ground on the regulator input / output |
| `PT-SW-L`, `PT-NOOUT`, `PT-MULTI-SRC` | info / warning | No inductor on the switch node / output rail unknown / rail driven by two regulators |
| `PT-DNP-LINK` | warning | A regulator input is reached only through a DNP part |
| `PT-DS-VIN-ABS` / `PT-DS-VOUT-ABS` | error | Input / output outside the datasheet absolute maximum |
| `PT-DS-VIN-REC` / `PT-DS-VOUT-REC` | warning | Outside the recommended range |
| `PT-DS-HEADROOM` | warning (info when met) | LDO input − output below the datasheet dropout |
| `PT-DS-EN-ABS` / `PT-DS-EN-LOW`, `PT-DS-EN` | error / warning, info | EN voltage outside its rating / below the turn-on level; level when met |
| `CR-PWR-NC`, `CR-PWR-UNSRC`, `CR-PWR-DNP` | error, warning | Power pin unconnected / its rail has no source / sourced only through a DNP part |
| `CR-PWR-GND`, `CR-GND-RAIL`, `CR-GND-MIX` | error, info | Supply pin on ground / ground pin on a supply / ground pins on several ground nets |
| `CR-DEC-NONE` | warning | IC supply net without a fitted capacitor to ground |
| `CR-OD-PULL` | warning | Open-drain / open-collector net with no pull resistor and no other driver |
| `CR-I2C-PULLUP`, `CR-I2C-RAIL` | error / warning | I2C line without pull-up / SDA and SCL pulled to different rails |
| `CR-I2C-ADDR`, `CR-I2C-UNRES` | error, info | Two devices at one address (same part and strapping, or same datasheet address) / address unknown |
| `CR-PIN-MISSING`, `CR-PIN-EXTRA`, `CR-PIN-NAME`, `CR-PIN-NC` | warning | Symbol pins vs the datasheet pin table; a leave-unconnected pin that shares a net |
| `CR-PIN-ABS` / `CR-PIN-REC` | error / warning | A pin or pin pair outside its absolute maximum / recommended limit |
| `CR-CAP-V` / `CR-CAP-DERATE`, `CR-CAP-FIELD` | error / warning | Capacitor DC voltage above its rating / above the derating fraction; `Voltage` field differs from the MPN's datasheet |
| `DC-CLAMP`, `DC-TRIP` | info, warning | A pin held at its internal clamp (current into it, and the source voltage at which its trip current is reached) / at or above the trip current (e.g. OVLO) |
| `DC-ZENER-P`, `DC-DRIVE` | info / warning / error, info | A conducting zener's current and power vs Ptot / a gate driver's sink or source current |
| `RT-L-ISAT`, `RT-L-IRMS` | error (warning above 80 % Isat), warning | Inductor peak current vs Isat, average vs the lowest Irms |
| `RT-CMC-I`, `RT-CONN-I` | error | Common-mode choke current vs I_rated; connector current per contact / per rail group |
| `RT-REG-IOUT`, `RT-REG-ILIM` | warning / error | Regulator output vs recommended IOUT / current limit; buck inductor peak vs the switch limit |
| `RT-TJ` | error / warning | Junction temperature above Tj max / within `tj_margin_c` of it |
| `RT-FET-ID`, `RT-D-IF`, `RT-LED` | error / warning | FET / series diode / LED current vs rating; CCR without headroom; LED with no limit |
| `RT-MODEL`, `RT-NODATA` | info | Regulator voltages unknown (current not carried up) / a part carries current but has no extracted rating |
| `PW-USBC-CC`, `PW-USBC-DATA` | error / warning | Type-C CC termination; D+ / D- of the two orientations not joined |
| `PW-VBUS-TVS`, `PW-CONN-GND`, `PW-ESD` | warning, info | Supply entry without clamp / without a ground pin; external signal nets without ESD clamp |
| `PW-SEQ-LOOP`, `PW-SEQ-SELF`, `PW-RESET` | error, warning / info | Enable loop / regulator enabled by a part it powers; reset net pull-up and filter cap |
| `PW-FB-IB`, `PW-FB-Z` | warning, info | FB bias current × R_top above 0.5 % of Vout; divider impedance above `fb_z_max_ohm` |
| `AU-BIAS`, `AU-HPF`, `AU-BLEED`, `AU-UNUSED` | error, warning | Op-amp input without DC path; coupling high-pass in the audio band; no bleed resistor; unused channel floating |
| `AD-VREF`, `AD-SUPPLY`, `AD-GND`, `AD-AAF` | warning, info | Converter reference / analog supply without cap; split grounds not joined; inputs without RC |
| `HP-JACK`, `LS-SAME`, `LS-OE` | warning, info | Jack driven directly from an output; level shifter on one rail / OE not driven |
| `DG-FLOAT`, `DG-PINTYPE`, `DG-BOOT` | error / warning, info | Input with no driver or unconnected; symbol pin type vs datasheet; boot strap without resistor |
| `DG-I2C-RPU`, `DG-SPI-CS`, `DG-LEVEL`, `DG-CLK-TERM`, `DG-USB-ESD`, `DG-SWD` | error / warning / info | I2C pull-up too strong; SPI CS shared / no pull-up; supply mismatch driver → receiver; clock fan-out without series R; USB data without ESD; debug pins not reachable |
| `ETH-*`, `HDMI-*`, `LVDS-TERM`, `MEM-*`, `WL-*` | error / warning / info | Interfaces: magnetics, termination, ESD, pull-ups, flash / DDR straps, antenna path |
| `CHG-*`, `BMS-*`, `PROT-CS`, `TF-*`, `SC-BAL`, `ISO-*` | error / warning / info | Power conversion: charger, BMS, isolated feedback, supercaps, isolation barrier |
| `MD-*`, `TC-FILTER`, `RTD-RREF`, `ALED-*` | error / warning / info | Motor drivers, temperature sensing, addressable LEDs |
| `RF-DCBLOCK`, `RF-MATCH`, `RF-MATCH-OPEN`, `CERT-*` | warning / info | RF paths and matching; certification hints |
| `LE-NORES`, `LE-ISET`, `DS-CAP`, `RTC-VBAT`, `KB-PULL`, `KB-GHOST` | warning / error / info | LED without limit; LED driver set pin open; display pump caps missing; RTC backup pin; button without pull-up; key matrix without diodes |

Parts of the rule vocabulary (pin-name sets, non-regulator exclusions) and the scope of the circuit detectors follow kicad-happy (MIT, Copyright (c) 2025 Andrew Klofas).

### Manufacturing outputs

- **`export_fab_package`** — one-shot: gerbers + drill (PTH/NPTH split) + pos CSV + BOM + 3D render, all under `<project>/fab/`
- **3D STEP export** — `export_step_3d` for Fusion 360 / SolidWorks / FreeCAD
- **3D render** — `render_pcb_3d` (PNG, configurable side / quality / rotation)
- **SVG per layer** — for documentation
- **BOM with sourcing fields** — `export_bom` carries the symbols' MPN / manufacturer / distributor fields
- **Panelization** — `panelize_board_grid` duplicates the board in a grid with mouse bites for batch fabrication

### Engineering analysis

- **Signal integrity** — IPC-2141A microstrip / stripline / differential / CPWG impedance, with inverse solver (target Z₀ → trace width)
- **Thermal capacity** — IPC-2152 trace current, both directions; per-net audit identifying the weakest segment
- **Thermal network** — lumped-element junction temperature solver (Rjc + Rca → Tj)
- **Crosstalk** — closed-form NEXT/FEXT for parallel microstrips
- **Return path continuity** — `check_return_path_continuity` checks each signal segment against the filled ground on the adjacent layer(s), skipping pad / via ends and power nets
- **EMC sanity checks** — `analyze_ground_coverage` (filled zone area on board outlines with rounded corners), `find_long_traces` (antenna detection), `validate_decoupling_caps` (cap placement distance)

### EMC pre-compliance review (`analyze_emc`)

Runs on the board's real filled copper: every zone's filled polygons are rasterised onto one grid (`resolution_mm`, default 0.5; even-odd fill, so fractured polygons with holes are exact) and all checks query it. Refill zones before running. Regulators, rail voltages and part data come from the schematic netlist, `analyze_power_tree` and the datasheet spec cache. It is a first-pass estimator for pre-compliance, not a sign-off.

- **`ground`** — each layer's role from its fill (a layer is a ground plane at ≥ 50 % ground fill, ≥ 80 % if it also carries signals — an inner GND layer is never mistaken for a signal layer), adjacent reference plane for every routed layer with the dielectric thickness, floating ground islands, every ground net and the parts that join them, signals crossing a change of reference-plane net without a bridging capacitor, ground-via stitching against λ/20 at `f_max_hz`, ground copper along the board edge, connector ground pins.
- **`switching`** — per switching regulator: switch-node copper area, the input hot loop through the real pad positions, the trapezoidal harmonic envelope, and a differential-mode radiation estimate (Ott, E = K·f²·A·I/r) against `standard` (`fcc-class-b`, `fcc-class-a`, `cispr-class-b`, `cispr-class-a`); switching inductors near the edge or a connector. Duty cycle from the rail voltages (buck only when the switch-node inductor lands on the output). The switching frequency is taken from `fsw={"U1": 500e3, "U2/ch1": 1.8e6}` only — it depends on RT/SYNC, so the datasheet value is shown as a hint, never used. Load current from `iout=`, else the datasheet IOUT max (worst case, said so).
- **`decoupling`** — every IC supply pin (power_in, from the netlist) to its nearest capacitor on the same net, each cap's ground pad to the nearest ground via, and per-rail capacitor banks against `pdn={"+3V3": 0.05}` or `{"ripple": 0.03, "i_step": 0.5}`. ESL / ESR only from extracted capacitor datasheets — never a generic per-package value.
- **`io`** — every connector line: TVS / ESD diode present and its distance to the pin, series filtering on signal lines; signal tracks near the edge; clocks near connectors; crystal-to-IC distance; diff-pair skew, layer changes and reference-plane continuity; clock / high-speed tracks closer than 3H to another net; connector aperture slot resonance (c / 2L, L from the connector's pad extent) against the known switching harmonics, and the shielding the radiation estimates leave to an enclosure.
- **`thermal`** — class-II MLCCs biased at ≥ 50 % of their `Voltage` rating (the capacitance loss only from an extracted datasheet, never a generic derating table) and ferrites within 10 mm of a switching regulator.
- **Test plan** — always returned: the tests to run for the standard, each switcher's harmonics in the conducted (150 kHz – 30 MHz) and radiated bands, near-field probe points (switch nodes, connectors), and the top risks.

| Rule | Severity | Meaning |
|---|---|---|
| `EM-GND-NONE` | error | No filled ground copper on any layer |
| `EM-ZONES-UNFILLED` | warning | Zones without fill in the file (refill and re-run) |
| `EM-STACK-REF` | warning | A routed layer has no adjacent plane |
| `EM-GND-ISLAND` | warning | Ground copper island with no via or pad |
| `EM-GND-DOMAINS` | info | Ground nets and the parts joining them |
| `EM-SPLIT-CROSS` | warning | Signal crosses a change of reference-plane net, no bridging cap within 5 mm |
| `EM-STITCH` | warning | < 90 % of the overlapping ground area within λ/40 of a ground via |
| `EM-EDGE-GND` | warning / info | Ground copper covers < 70 % / < 90 % of the board edge |
| `EM-CONN-GND` | warning / info | Connector with fewer than max(2, signals/4) ground pins (warning for high-speed) |
| `EM-SW-AREA` | warning / info | Switch-node copper > 100 / ≥ 25 mm² |
| `EM-SW-CIN` | warning | Nearest input cap > 5 mm from the VIN pin |
| `EM-SW-RAD` | warning / info | Radiation estimate margin < 6 dB / margin, or why it was skipped |
| `EM-SW-FSW`, `EM-SW-NONET` | info | No switching frequency given / no netlist |
| `EM-SW-L-EDGE`, `EM-SW-L-CONN` | warning | Switching inductor < 3 mm from the edge / < 5 mm from a connector |
| `EM-DEC-DIST`, `EM-DEC-NONE` | warning | Supply pin's nearest cap beyond `decap_max_mm` / no cap on that net |
| `EM-DEC-VIA` | info | Cap ground pad farther than `cap_via_max_mm` from a ground via |
| `EM-PDN` | info | Rail bank vs target impedance (capacitive asymptote; ESL coverage stated) |
| `EM-IO-ESD`, `EM-IO-ESD-DIST` | warning (info on power lines) | Connector line without TVS / ESD diode / protection farther than `esd_max_mm` |
| `EM-IO-FILTER` | info | Signal line leaves the board with no series R / ferrite / inductor |
| `EM-EDGE-TRACE` | warning / info | Signal track within `edge_keepout_mm` of the edge (warning for clock / high-speed) |
| `EM-CLK-CONN` | warning | Clock passes < 10 mm from a connector |
| `EM-XTAL-DIST` | warning | Crystal > 10 mm from the IC it drives |
| `EM-DIFF-SKEW`, `EM-DIFF-REF` | warning | Pair skew > `diff_skew_ps` / reference plane changes net or has gaps under the pair |
| `EM-DIFF-LAYERS` | info | Pair routed on more than one layer |
| `EM-3H` | warning | Clock / high-speed track parallel to another net closer than 3H |
| `EM-SHIELD-SLOT`, `EM-SHIELD-SE` | warning, info | Connector aperture resonance near a known harmonic / shielding needed for 6 dB margin |
| `EM-TH-BIAS`, `EM-TH-FERRITE` | info | Class-II MLCC at ≥ 50 % of rating / ferrite < 10 mm from a switcher |

### Simulation

- **SPICE** — `export_spice_netlist` + `run_ngspice_simulation` (transient / DC / AC / noise)
- *Note: requires symbols with `Spice_*` fields and `ngspice` installed (`brew install ngspice`)*
- **Automatic subcircuit checks** — `simulate_subcircuits` finds the passive networks in the schematic itself (no `Spice_*` fields needed) and checks them against the design intent:
  - `divider` — resistor dividers into an active pin: ratio, tapped voltage, worst case from each resistor's `Tolerance` field; for a regulator FB divider, Vout typ and worst case from the datasheet Vref min / max in the spec cache, plus Monte Carlo (`monte_carlo=N`);
  - `rc` — RC low-pass nodes feeding an active input (not supply rails): corner frequency with the node's Thevenin resistance;
  - `lc` — LC / ferrite / π ladders (series L between nodes with capacitors to ground; converter switch nodes excluded), with R–C damping branches, run as an ngspice AC sweep from the ladder's source end to its far end: resonance peaks and attenuation at `freqs` and at the 1st–3rd harmonic of each `fsw`; Monte Carlo over L / C tolerances; optional `loads={net: Ω}` and `source_ohm`.

  - `opamp` — op-amp stages from pin names (`+IN` / `IN+` / `1IN-` / `-INA` / `OUTA` …): follower, non-inverting or inverting, gain and noise gain from the feedback network, feedback-capacitor pole, and the closed-loop bandwidth GBW / noise gain when the spec cache has the op-amp's `GBW`. With a vendor model — `opamp_models={"U1": {"lib": "OPA1612.lib", "subckt": "OPA1612", "pins": ["+IN", "-IN", "V+", "V-", "OUT"]}}` (roles in `.subckt` node order, else read from the node names) or the symbol's KiCAD `Sim.Library` / `Sim.Name` / `Sim.Pins` fields — each stage is simulated with ngspice AC on its real feedback network and supplies (rail voltages or `supplies=`): gain, −3 dB frequency, peaking; without one the amplifier is ideal and the result says so;
  - `crystal` — a crystal with a load cap on each pin: effective load C1·C2/(C1+C2) + `c_stray_pf` (no stray value is assumed) against the crystal's `CL` from the spec cache (warning beyond ±10 %).

  Inductor DCR, capacitor ESR / ESL and L / C tolerances come from the datasheet spec cache when a part has them (a `Tolerance` field wins), and a capacitor with a `C_bias` curve is derated at its rail's DC voltage (`dc_bias_ratio` in the result); `pcb_parasitics=True` adds each ladder capacitor's mounting inductance: the ground-via barrel from its layer to the first ground plane (L = 0.2·h·(ln(4h/d) + 1) nH) plus the pad-to-via connection as a strip over the plane (track width, or the pad width for a pour; inductance per length from Hammerstad's microstrip Z0). Otherwise parts are ideal — with an open load the peaking is an upper bound and is reported as info. Rules: `SP-DIV-VOUT`, `SP-TOL` (no Tolerance field), `SP-LC-PEAK` (peaking above `peaking_warn_db`, warning only with loads given), `SP-LC-ATT`, `SP-OPAMP`, `SP-XTAL-CL`, `SP-NOSIM` (no ngspice).

### Release: Gerbers, diff, what-if, lifecycle, BOM, gate

- **`analyze_gerbers`** — reads the exported Gerber (RS-274X / X2) and Excellon folder as the fab will: per file the function, polarity, apertures, draws / flashes / regions and extent; per drill file the tool table with hits. Cross-checks the `.gbrjob`: files listed but missing (`GB-MISSING-FILE`, error only for fabrication layers), copper layer count (`GB-LAYERS`), outline extent vs board size (`GB-SIZE`); missing outline / drill (`GB-NO-OUTLINE`, `GB-NO-DRILL`), holes outside the outline (`GB-DRILL-OUTSIDE`), Gerbers older than the board file (`GB-STALE`), and with `preset` the narrowest conductor draw and smallest drill against the fab minimum (`GB-MIN-TRACE`, `GB-MIN-DRILL`).
- **`diff_designs`** — two revisions of the active project (`git:<ref>`, a folder, or the working tree; git refs go to a temporary folder, read-only). Schematic, from KiCAD's netlists: parts added / removed, Value / Footprint / lib / DNP / field changes, net renames, pins moved to another net, nets added / removed. PCB: footprints added / removed / moved / rotated / flipped / re-footprinted, routing changes per net (length, segments), vias, zones per net and layer, board size.
- **`what_if`** — `changes={"R7": "120k", "C42": {"dnp": true}}` re-runs `analyze_power_tree`, `review_connectivity`, the divider / RC checks, (with ngspice) the LC / π ladders and (with `emc=True`) the netlist-driven `analyze_emc` groups on the unchanged copper, on a changed copy of the netlist (no file touched): regulator Vout and enable changes, rail voltages, divider outputs, RC corners, filter peaking and attenuation at the `fsw` harmonics, switcher radiation margins, and the findings that appear or disappear.
- **`audit_lifecycle`** — every schematic MPN at DigiKey / Mouser / element14-Farnell (exact match; `jlcpcb` adds JLC stock): lifecycle (DigiKey `ProductStatus` + Discontinued / EndOfLife, Mouser `LifecycleStatus` + `SuggestedReplacement`, Farnell `productStatus`) and stock. `LC-EOL` (error; includes Farnell `NO_LONGER_MANUFACTURED`), `LC-NRND`, `LC-RESTRICTED`, `LC-STOCK` (warnings), `LC-SINGLE`, `LC-NOTFOUND`, `LC-DIST-DROP` (info: a distributor stopped stocking it, `NO_LONGER_STOCKED`).
- **`review_assembly`** — real (also non-convex) courtyard outlines that overlap (`AS-CRTYD-OVERLAP`; touching is fine) or sit closer than `min_spacing_mm` (`AS-CRTYD-SPACING`), courtyards beyond or near the board edge (`AS-EDGE`, info for connectors), no courtyard (`AS-NO-CRTYD`), SMD parts on a side the assembler will not populate (`assembly_sides`, `AS-SIDE` error), through-hole parts (`AS-THT`), fiducials per populated side (`AS-FIDUCIAL`), polarized parts with no silkscreen of their own (`AS-SILK-POLARITY`) or no asymmetric mark at pad 1 — a silk point near pad 1 whose point reflection through the pads' centre has no counterpart: pin-1 dot, cathode bar, chamfer (`AS-PIN1-MARK`); part heights from each footprint's 3D STEP model (offset / scale / rotation applied; `KICAD{N}_3DMODEL_DIR` resolved): tall parts near the edge (`AS-TALL-EDGE`), parts above `max_height_top_mm` / `max_height_bottom_mm` (`AS-HEIGHT`, enclosure clearance), model paths that do not resolve (`AS-3D-MISSING`), no model (`AS-3D-NONE`) — heights are the model's, not the fitted part's datasheet; and schematic ↔ PCB cross-verify: parts on one side only (`XV-MISSING` / `XV-EXTRA`), symbol pins without a pad of that number (`XV-PADS`, error), Value / Footprint / MPN differing between schematic and board (`XV-FIELD`).
- **`diff_bom`** — the BOM of two revisions (`git:<ref>`, a folder, or the working tree): designators added / removed, Value / Footprint / MPN / Manufacturer / LCSC / distributor numbers / DNP / exclude-from-BOM changes (`extra_fields` for more), quantity changes per BOM line (MPN, or value + footprint), MPNs new or gone.
- **`export_fab_bom`** — assembly BOM + CPL in the fab's columns: `jlcpcb` (`Comment, Designator, Footprint, LCSC Part #`; `FB-NO-LCSC`) or `pcbway` (`Item #, Designator, Qty, Manufacturer, Mfg Part #, Description / Value, Package/Footprint, Type, Your Instructions / Notes`; `FB-NO-MPN`, `FB-NO-MFR`), CPL `Designator, Mid X, Mid Y, Layer, Rotation` from `kicad-cli pcb export pos`. DNP, excluded-from-BOM, test points, holes, fiducials and `exclude_refs` (consigned parts) are left out. Rotation offsets: `rotation_offsets={"^SOT-23": 180}` (footprint regex) or a `JLCPCB Rotation Offset` / `Rotation Offset` symbol field — no fab table is assumed.
- **`update_symbol_fields`** — set fields on many symbols across the whole sheet hierarchy from `updates={"R7": {"MPN": "...", "Manufacturer": "..."}}` and / or a CSV (Reference / Designator column, one column per field). Dry run first (old → new list); `dry_run=False` writes each changed sheet through the guarded writer, every unit of a multi-unit symbol. Reused sheets are reported (a field is shared by every instance); `Reference` is refused.
- **`fab_release_gate`** — one verdict, `FAB-READY` or `NOT FAB-READY`, with blockers and warnings: ERC errors; DRC errors, unconnected items and schematic parity (run without refilling or saving); errors from the power tree and connectivity reviews; the Gerber folder (errors, stale Gerbers, preset minimums); MPN coverage (blocking only with `require_mpn`); assembly and cross-verify errors; optionally the lifecycle audit (`LC-EOL` blocks). Read-only.

### Multi-board projects

- `add_board`, `set_active_board`, `list_boards` — manage several `.kicad_pcb` files in one project (main + breakout + debugger), every PCB tool honors the active board.

---

## A non-trivial example, end to end

```
# 1. Schematic with auto-annotation
# Y grows downwards, like the KiCAD GUI: +5V on top, GND at the bottom.
add_power_symbol  net="+5V"   x_mm=101.6  y_mm=50.8
add_symbol        lib_id="Device:R" reference="R?" value="10k" x_mm=101.6 y_mm=76.2
add_symbol        lib_id="Device:R" reference="R?" value="1k"  x_mm=101.6 y_mm=101.6
add_power_symbol  net="GND"   x_mm=101.6  y_mm=127
annotate_schematic                                        # R? → R1, R2

# wire pins (using get_pin_position to compute exact endpoints)
add_wire ...  add_wire ...  add_wire ...

# 2. PCB with JLCPCB rules + USB diff pair + GND plane
set_board_outline width_mm=50 height_mm=30
set_layer_count   n=4
apply_fab_preset  preset="jlcpcb_2l_default"
add_diff_pair_class name="USB" diff_pair_width_mm=0.20 diff_pair_gap_mm=0.18
add_net_class       name="Power" track_width_mm=0.5 clearance_mm=0.25
assign_net_class    net_pattern="USB_*"  class_name="USB"
assign_net_class    net_pattern="+5V"    class_name="Power"

add_footprint   lib_id="Resistor_SMD:R_0603_1608Metric" reference="R1" ...
add_footprint   lib_id="Resistor_SMD:R_0603_1608Metric" reference="R2" ...
update_pcb_from_schematic                                  # nets propagate
add_ground_plane  layer="B.Cu"  net_name="GND"
autoroute_pcb     passes=20

# 3. Validate + export
run_erc                                                    # 0 errors
run_drc           refill_zones=True  schematic_parity=True  # 0 errors
export_fab_package  include_render=True
enrich_bom_with_sourcing  sources="mouser,digikey,tme,farnell"
```

---

## Architecture

```
kicad-claude-mcp/
├── server.py                          # FastMCP entry point — registers all tools
├── pyproject.toml                     # uv project
├── .env.example                       # API keys + path overrides
├── src/kicad_claude/
│   ├── strict_args.py                 # unknown-argument errors + accepted aliases
│   ├── state.py                       # active project / sheet / board
│   ├── tools/                         # MCP tools, thin wrappers over adapters
│   │   ├── project.py                 # create_project, set_project, list_components, clear_stale_lock
│   │   ├── review.py                  # analyze_power_tree, review_connectivity, review_circuits, review_part_ratings
│   │   ├── bom.py                     # diff_bom, export_fab_bom, update_symbol_fields
│   │   ├── release.py                 # analyze_gerbers, diff_designs, what_if, audit_lifecycle, review_assembly, fab_release_gate
│   │   ├── datasheets.py              # sync_datasheets, extraction task / save / specs
│   │   ├── library.py                 # index_libraries, search_*, get_symbol_details
│   │   ├── schematic.py               # add_symbol, wires, hierarchical sheets, buses
│   │   ├── pcb.py                     # PCB editing, multi-board, layers
│   │   ├── routing.py                 # autoroute_pcb (Freerouting wrapper)
│   │   ├── manufacturing.py           # gerbers, drill, BOM, render, fab_package, STEP
│   │   ├── validation.py              # run_erc, run_drc with JSON parsing
│   │   ├── rules.py                   # design rules, net classes, fab presets, DRU
│   │   ├── sourcing.py                # availability, fetch_datasheet, vendor ZIPs, BOM enrichment
│   │   ├── sync.py                    # annotate, update_pcb_from_schematic
│   │   ├── library_create.py          # create_symbol, create_footprint
│   │   ├── signal_integrity.py        # impedance calculators
│   │   ├── thermal.py                 # IPC-2152 current capacity
│   │   ├── rf.py                      # via arrays, ground stitching, RF microstrip
│   │   ├── emc.py                     # EMC heuristics + analyze_emc
│   │   ├── simulation.py              # thermal network, crosstalk, return path
│   │   ├── spice.py                   # ngspice wrapper + simulate_subcircuits
│   │   └── panelization.py            # grid panelize + mouse bites
│   ├── adapters/                      # logic detached from MCP framing
│   │   ├── sch_io.py                  # parse + write
│   │   ├── kicad_prettify.py          # port of KiCAD's Prettify (byte-exact layout)
│   │   ├── safe_write.py              # lock check, backup, re-parse, restore
│   │   ├── sch_editor.py              # schematic tree mutations
│   │   ├── sch_netlist.py             # schematic connectivity model
│   │   ├── pcb_editor.py              # PCB tree mutations
│   │   ├── pcb_geometry.py            # outlines, zones, polygons
│   │   ├── kicad_cli.py               # subprocess wrapper for kicad-cli
│   │   ├── kicad_python.py            # bridge to KiCAD's bundled Python (pcbnew)
│   │   ├── freerouting.py             # JAR runner with timeout + log parser
│   │   ├── power_review.py            # regulators, rails, Vout, datasheet limit checks
│   │   ├── connectivity_review.py     # power pins, pull-ups, I2C, pinout, abs max, caps
│   │   ├── circuit_review.py          # power / analog / digital / misc circuit detectors
│   │   ├── domain_review.py           # interfaces / power conversion / drives / RF + certification
│   │   ├── part_ratings.py            # per-rail current model, ratings, Tj, LED strings
│   │   ├── dc_model.py                # DC operating point: resistor networks, clamps, zeners, switch scenarios
│   │   ├── bom_tools.py               # BOM diff, JLCPCB / PCBWay BOM + CPL, field write-back
│   │   ├── emc_review.py              # EMC review on rasterised copper + test plan
│   │   ├── spice_auto.py              # divider / RC / LC-pi detection, ngspice AC, Monte Carlo
│   │   ├── gerber_review.py           # Gerber / Excellon / gbrjob analysis
│   │   ├── design_diff.py             # schematic + PCB diff, git snapshots
│   │   ├── assembly_review.py         # courtyards, sides, fiducials, schematic <-> PCB cross-verify
│   │   ├── datasheet_index.py         # manifest, PDF text, MPN <-> PDF matching
│   │   ├── datasheet_specs.py         # spec schema, validation, cache
│   │   ├── datasheet_fetch.py         # PDF download with fallback
│   │   ├── digikey.py / mouser.py / farnell.py / tme.py / jlcpcb.py   # sourcing clients
│   │   ├── electrical_calc.py         # impedance + IPC-2152 formulas
│   │   ├── thermal_emc.py             # closed-form thermal/EMC math
│   │   ├── panelization.py            # tree duplication + translation
│   │   ├── library_create.py          # symbol/footprint synthesis
│   │   ├── annotation.py              # auto-numbering across sheets
│   │   ├── drc_rules.py               # .kicad_dru read/write
│   │   ├── project_settings.py        # .kicad_pro JSON helpers
│   │   ├── vendor_import.py           # ZIP extraction + lib-table updates
│   │   ├── snapeda.py                 # URL helpers + manual fallback
│   │   └── length_tuning.py           # meander geometry generator
│   ├── indexer/                       # KiCAD library indexing (~10 MB cache)
│   ├── templates/                     # blank project / sheet / PCB
│   └── utils/                         # logging, geometry (mm, grid snap), paths
├── tests/                             # 642 fast + 61 acceptance + 6 network tests
└── docs/open_points.md                # the open-items queue
```

Open work and new field reports live in [`docs/open_points.md`](./docs/open_points.md).

---

## Caveats and gotchas

- **Logging must go to stderr.** STDIO MCP transports use stdout for JSON-RPC; any `print()` to stdout breaks the connection. Use `from kicad_claude.utils.logging import setup_logging`.
- **Close KiCAD before mutating files.** KiCAD locks `.kicad_*` files while open and can overwrite the changes you make; the server refuses to write while the lock exists. Open it to verify, close before the next round of edits.
- **Coordinates are KiCAD-native everywhere** — millimetres, Y down, origin at the page's top-left, the numbers the KiCAD GUI shows (`utils/geometry.py`). Schematic placements snap to the 1.27 mm grid.
- **`kicad-cli` is found automatically**, from `KICAD_CLI`, then `PATH`, then the standard install paths (`%ProgramFiles%\KiCad\<version>\bin` newest first on Windows, the app bundle on macOS, `/usr/bin` on Linux). `run_erc` / `run_drc` report which binary they used in `cli_path`.
- **Backups** go to `<project>/.backups/<timestamp>_<file>`; the newest 10 per file are kept. Add `.backups/` to `.gitignore`.
- **Freerouting needs Java 21+.** Freerouting 2.2.x requires Java 25; we ship 2.1.0 because it works on Java 21–24.
- **Closed-form analyses are estimators.** Impedance / current / crosstalk / thermal / return-path tools give ballpark numbers good for early design. Production hardware needs Ansys / Sonnet / Saturn PCB / Icepak for sign-off.
- **Design review is only as good as its inputs.** Datasheet checks run only for parts with an extracted spec record; a net's voltage is known only from regulator outputs, series links, resistor dividers and the `rail_voltages` you pass — nodes behind switches are skipped (see `docs/open_points.md`).

---

## Development

```bash
uv run mcp dev server.py                          # MCP Inspector (browser UI)
uv run pytest -m "not slow and not network" -q    # 642 fast tests, ~1 min
uv run pytest -m "slow" -q                        # 50 acceptance tests via kicad-cli (~8 min)
uv run pytest -m "network" -q                     # 6 live distributor tests
```

The slow tests need KiCAD installed and the library index built (`index_libraries`
once). Network tests need `.env` with valid DigiKey OAuth credentials and a Mouser
*Search API* key (Mouser issues two keys per account — Search and Order — only
the Search one works here).

---

## Acknowledgements

Built on top of:

- [`kicad-skip`](https://github.com/psychogenic/kicad-skip) for some schematic operations
- [`sexpdata`](https://pypi.org/project/sexpdata/) for parsing KiCAD's S-expression format
- [`Freerouting`](https://github.com/freerouting/freerouting) for autorouting
- KiCAD's bundled `pcbnew` Python API for Specctra DSN/SES interchange and netlist application
- `kicad-cli` for ERC, DRC, and manufacturing exports
- DigiKey V4 API, Mouser V2 API, element14 / Farnell, TME and JLCPCB for live sourcing data
- [`pypdf`](https://pypi.org/project/pypdf/) for datasheet text
- kicad-happy (MIT, Copyright (c) 2025 Andrew Klofas) — review rule ideas and pin-name vocabulary

---

## License

MIT — see [`pyproject.toml`](./pyproject.toml).
