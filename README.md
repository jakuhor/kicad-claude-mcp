# kicad-claude-mcp

> Design KiCAD PCBs from natural language. An MCP server that gives Claude
> Code (or any MCP client) **110 tools** to drive a complete PCB workflow —
> schematic capture, library management, PCB layout, autorouting, manufacturing
> outputs, signal-integrity calculations, RF design, and more.

**110 MCP tools · 268 tests (236 fast, 28 acceptance, 3 network) · 16 phases · headless from idea to gerbers**

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
- KiCAD 9.0+ or 10.x (with `kicad-cli` installed)
- Java 21+ (for autorouting via Freerouting; Java 24 works, Java 25 needed for the very latest Freerouting)
- *Optional*: `ngspice` (for SPICE simulation), DigiKey/Mouser API keys (for live BOM enrichment)

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

### Schematic capture

- **Symbol placement & connections**: `add_symbol`, `add_wire`, `add_label`, `add_power_symbol`, `add_no_connect`, `move_symbol`, `remove_symbol`, `get_pin_position`, `list_pins`
- **Editing and deletion**: `remove_wire`, `add_junction`, `remove_junction`, `remove_no_connect`, `remove_items_in_box` (both endpoints must be inside, so a crossing wire survives), and `remove_symbol(remove_connected_wires=True)` to drop the stubs left on the removed pins
- **Grid discipline**: every placement snaps to KiCAD's 1.27 mm grid unless you pass `snap_to_grid=False`, so endpoints actually connect
- **Hierarchical sheets**: `add_sheet`, `set_active_sheet`, `add_hierarchical_label`, `add_sheet_pin`, `list_sheets` — proper multi-level designs
- **Buses**: `add_bus`, `add_bus_entry`, `add_bus_alias` — visual grouping of address/data lines
- **Annotation**: `annotate_schematic` — auto-numbers `R?` → `R1, R2, ...` across all sheets
- **Custom symbols**: `create_symbol` with full pin specifications

### Library management

- **22,000+ KiCAD official symbols indexed** — `index_libraries`, `search_symbol`, `search_footprint`, `get_symbol_details`
- **External sourcing**: `find_or_fetch_symbol`, `import_vendor_zip` (SnapEDA / Ultra Librarian), `check_availability` (DigiKey + Mouser)
- **Custom footprints**: `create_footprint` with auto-courtyard and silk outline

### PCB layout

- **2 to 32 copper layers** with auto-generated stackup (`set_layer_count`)
- **Footprint placement**: `add_footprint`, `move_footprint`, `place_footprints_grid`
- **Routing**: `add_track`, `add_via`, `add_via_array`, `add_meander` (length tuning)
- **Copper pours**: `add_zone`, `add_ground_plane` (auto-extracts board outline)
- **Mechanical**: `add_mounting_hole` (M2/M2.5/M3/M4/M5), `add_fiducial`, `add_silk_text`
- **RF specials**: `add_rf_microstrip` (target Z₀), `add_ground_stitching`
- **Sync**: `update_pcb_from_schematic` propagates net assignments via `pcbnew`

### Autorouting

- **Freerouting integration** — `autoroute_pcb` runs the full pipeline (DSN export → routing → SES import) with timeout, parses stats from output
- **Diff pair coupling** — auto-detected by net naming (`_P/_N`, `+/-`, `DP/DM`); routed coupled when net class has `diff_pair_width`/`gap` set

### Validation

- **ERC and DRC** — `run_erc`, `run_drc` shell out to `kicad-cli` and parse the JSON
- **Custom DRC rules** — `add_drc_rule` writes `.kicad_dru` with constraint types (clearance, track_width, length, skew, diff_pair_gap, …) and conditions in KiCAD's expression language
- **Net classes + fab presets** — `apply_fab_preset` for JLCPCB / PCBWay / OSH Park; `add_net_class` for Power / USB / HDMI / Ethernet

### Manufacturing outputs

- **`export_fab_package`** — one-shot: gerbers + drill (PTH/NPTH split) + pos CSV + BOM + 3D render, all under `<project>/fab/`
- **3D STEP export** — `export_step_3d` for Fusion 360 / SolidWorks / FreeCAD
- **3D render** — `render_pcb_3d` (PNG, configurable side / quality / rotation)
- **SVG per layer** — for documentation
- **BOM enriched in real-time** — `enrich_bom_with_sourcing` queries DigiKey + Mouser per unique value, appends MPN / stock / price / URL columns
- **Panelization** — `panelize_board_grid` duplicates the board in a grid with mouse bites for batch fabrication

### Engineering analysis

- **Signal integrity** — IPC-2141A microstrip / stripline / differential / CPWG impedance, with inverse solver (target Z₀ → trace width)
- **Thermal capacity** — IPC-2152 trace current, both directions; per-net audit identifying the weakest segment
- **Thermal network** — lumped-element junction temperature solver (Rjc + Rca → Tj)
- **Crosstalk** — closed-form NEXT/FEXT for parallel microstrips
- **Return path continuity** — heuristic check that signal traces have GND plane underneath
- **EMC sanity checks** — ground coverage %, long-trace antenna detection, missing decoupling caps

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
enrich_bom_with_sourcing  sources="digikey,mouser"
```

---

## Architecture

```
kicad-claude-mcp/
├── server.py                          # FastMCP entry point — registers all tools
├── pyproject.toml                     # uv project + 110 tool surface
├── .env.example                       # API keys + path overrides
├── src/kicad_claude/
│   ├── tools/                         # MCP tools (one file per phase)
│   │   ├── project.py                 # create_project, set_project, list_components
│   │   ├── library.py                 # index_libraries, search_*, get_symbol_details
│   │   ├── schematic.py               # add_symbol, wires, hierarchical sheets, buses
│   │   ├── pcb.py                     # PCB editing, multi-board, layers
│   │   ├── routing.py                 # autoroute_pcb (Freerouting wrapper)
│   │   ├── manufacturing.py           # gerbers, drill, BOM, render, fab_package, STEP
│   │   ├── validation.py              # run_erc, run_drc with JSON parsing
│   │   ├── rules.py                   # design rules, net classes, fab presets, DRU
│   │   ├── sourcing.py                # DigiKey, Mouser, vendor ZIPs, BOM enrichment
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
│   │   ├── sch_io.py                  # parse + byte-exact KiCAD 10 formatting
│   │   ├── sch_editor.py              # schematic tree mutations
│   │   ├── pcb_editor.py              # PCB tree mutations
│   │   ├── kicad_cli.py               # subprocess wrapper for kicad-cli
│   │   ├── kicad_python.py            # bridge to KiCAD's bundled Python (pcbnew)
│   │   ├── freerouting.py             # JAR runner with timeout + log parser
│   │   ├── digikey.py / mouser.py     # OAuth2 / API key clients
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
├── tests/                             # 236 fast + 28 acceptance + 3 network tests
└── docs/                              # investigation notes
```

[`docs/issue8_formatting_probe.md`](./docs/issue8_formatting_probe.md) records how
the writer was measured against KiCAD 10's own output and which formatting rule
each line of `sch_io.dumps` implements.

---

## Caveats and gotchas

- **Logging must go to stderr.** STDIO MCP transports use stdout for JSON-RPC; any `print()` to stdout breaks the connection. Use `from kicad_claude.utils.logging import setup_logging`.
- **Close KiCAD before mutating files.** KiCAD locks `.kicad_*` files while open and can overwrite the changes you make. Open it to verify, close before next round of edits.
- **Two coordinate conventions.** Schematic tools take KiCAD-native coordinates — millimetres, Y down, origin at the page's top-left, the numbers the KiCAD GUI shows — and snap to the 1.27 mm grid. PCB tools still take Y *up*, flipped around the page height; that side is due to move to native coordinates. Both live in `utils/geometry.py`. If items land in unexpected places, suspect this first.
- **`kicad-cli` is found automatically**, from `KICAD_CLI`, then `PATH`, then the standard install paths (`%ProgramFiles%\KiCad\<version>\bin` newest first on Windows, the app bundle on macOS, `/usr/bin` on Linux). `run_erc` / `run_drc` report which binary they used in `cli_path`.
- **Mutating tools write a backup first** to `<project>/.backups/<timestamp>_<file>`. The directory grows with every call; clean it out or add it to `.gitignore`.
- **Freerouting needs Java 21+.** Freerouting 2.2.x requires Java 25; we ship 2.1.0 because it works on Java 21–24.
- **Closed-form analyses are estimators.** Impedance / current / crosstalk / thermal / return-path tools give ballpark numbers good for early design. Production hardware needs Ansys / Sonnet / Saturn PCB / Icepak for sign-off.
- **Symbols with `extends`** (like `Device:R_Small` extending `Device:R`) currently inject only the extending symbol; the base isn't pulled. Use the canonical name (`Device:R`) for now.

---

## Development

```bash
uv run mcp dev server.py                          # MCP Inspector (browser UI)
uv run pytest -m "not slow and not network" -q    # 236 fast tests, ~13 s
uv run pytest -m "slow" -q                        # 28 acceptance tests via kicad-cli
uv run pytest -m "network" -q                     # 3 live DigiKey/Mouser tests
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
- DigiKey V4 API and Mouser V2 API for live sourcing data

---

## License

MIT — see [`pyproject.toml`](./pyproject.toml).
