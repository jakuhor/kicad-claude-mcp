# kicad-claude-mcp

> Design KiCAD PCBs from natural language. An MCP server that gives Claude
> Code (or any MCP client) **143 tools** to drive a complete PCB workflow —
> schematic capture, library management, PCB layout, autorouting, design
> review against the parts' datasheets, sourcing, manufacturing outputs,
> signal-integrity calculations, RF design, and more.

**143 MCP tools · 621 tests (565 fast, 50 acceptance, 6 network) · headless from idea to gerbers**

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
- **Structured specs, extracted by the calling agent**: `get_datasheet_extraction_task` hands over the PDF path, the text of the pages holding the tables, the part's schematic pins and nets, and the schema; `save_datasheet_extraction` validates the record and caches it in `datasheets/specs/<MPN>.json`. Every value carries its PDF page and SI unit; the server checks each number against the cited page's text and warns when it is not there. A record goes `stale` when its PDF changes. `get_datasheet_specs` returns one record or the coverage. No value is taken from general knowledge.
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

- **`analyze_power_tree`** — finds each regulator (one record per channel on multi-channel parts), its topology, input and output rail, EN state (tied to input, divider, driven by another part's pin = sequencing, floating …), power-good pull-up, feedback divider (values, ratio), SET resistor, switch-node inductor and input / output capacitors. Builds the rail tree through series L / ferrite / 0 Ω links (DNP marked). Computes each Vout from the datasheet specs — FB divider with the right-polarity Vref, ANY-OUT pin strapping, or SET current × R — or from a caller `vref`, and carries rail voltages through the links. With those voltages it checks VIN against absolute maximum and recommended range, VOUT range, LDO headroom against dropout, and the EN pin voltage (divider / pull-up, power-good released) against the EN rating and turn-on threshold. Unregulated inputs (a USB-PD contract) come from `rail_voltages={"/VBUS": 20}`.
- **`review_connectivity`** — checks (all on by default, choose with `checks=`): IC power pins and how each rail is sourced; decoupling per IC supply net; open-drain outputs without pull-up; I2C buses (pull-ups, address strapping, fixed datasheet addresses); symbol pins against the datasheet pin table; every pin and pin pair (VDS, VGS, TVS standoff) against its absolute maximum at the net's voltage; capacitor DC voltage against its `Voltage` field (`cap_derating`, default 0.8).

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
| `CR-CAP-V` / `CR-CAP-DERATE` | error / warning | Capacitor DC voltage above its rating / above the derating fraction |

Parts of the rule vocabulary (pin-name sets, non-regulator exclusions) follow kicad-happy (MIT, Copyright (c) 2025 Andrew Klofas).

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

### Simulation

- **SPICE** — `export_spice_netlist` + `run_ngspice_simulation` (transient / DC / AC / noise)
- *Note: requires symbols with `Spice_*` fields and `ngspice` installed (`brew install ngspice`)*

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
│   │   ├── review.py                  # analyze_power_tree, review_connectivity
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
│   │   ├── emc.py                     # heuristic EMC checks
│   │   ├── simulation.py              # thermal network, crosstalk, return path
│   │   ├── spice.py                   # ngspice batch wrapper
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
├── tests/                             # 565 fast + 50 acceptance + 6 network tests
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
uv run pytest -m "not slow and not network" -q    # 565 fast tests, ~1 min
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
