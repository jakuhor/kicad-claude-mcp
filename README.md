# kicad-claude-mcp

> Design KiCAD PCBs from natural language. An MCP server that gives Claude
> Code (or any MCP client) **154 tools**, loaded in groups on demand, to drive a complete PCB workflow —
> schematic capture, library management, PCB layout, autorouting, design
> review against the parts' datasheets, sourcing, manufacturing outputs,
> signal-integrity calculations, RF design, and more.

**154 MCP tools in 11 on-demand sets · 1073 tests (995 fast, 72 acceptance, 6 network) · headless from idea to gerbers**

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
- KiCAD 10.x (with `kicad-cli` installed) — files are written in the formats KiCAD 10.0.8 writes; KiCAD 10 or newer is supported
- Java 25+ (for autorouting via Freerouting 2.5.0)
- *Optional*: `ngspice` or LTspice (for SPICE simulation); DigiKey / Mouser / Farnell / TME API keys (live sourcing and datasheet lookup — the JLCPCB source needs no key)

### Install

```bash
git clone https://github.com/Greg3001/kicad-claude-mcp.git
cd kicad-claude-mcp
uv sync
cp .env.example .env       # fill API keys when ready (optional, sourcing only)
```

Download Freerouting 2.5.0 to `third_party/freerouting.jar` (only needed for `autoroute_pcb`):

```bash
mkdir -p third_party
curl -L https://github.com/freerouting/freerouting/releases/download/v2.5.0/freerouting-2.5.0.jar \
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

Register the server for your user (all projects):

```bash
claude mcp add --scope user kicad -- \
  uv run --frozen --directory /ABSOLUTE/PATH/TO/kicad-claude-mcp python server.py
claude mcp add --scope user kicad-dev -- \
  uv run --frozen --directory /ABSOLUTE/PATH/TO/kicad-claude-mcp-dev python server.py
```

`uv run --directory` runs the checkout's own `.venv`, so the system Python
never needs the dependencies. On Windows give the path as
`C:/path/to/kicad-claude-mcp` (forward slashes work). Restart Claude Code, run `/mcp` to confirm
`kicad` is connected, then ask for `Use the kicad ping tool`.

The second entry is optional — it is the branch checkout from the previous
section, exposed under its own name. Drop it if you only run one.

Other MCP clients take the same command as JSON (for Claude Code's project
scope, a `.mcp.json` in the project root):

```json
{
  "mcpServers": {
    "kicad": {
      "command": "uv",
      "args": ["run", "--frozen", "--directory", "/ABSOLUTE/PATH/TO/kicad-claude-mcp",
               "python", "server.py"]
    }
  }
}
```

The server starts with the `core` tool set only and the model loads the rest
with `load_toolset` (see [Tool sets](#tool-sets)). To register everything at
startup instead, add `-e KICAD_MCP_TOOLSETS=all` to `claude mcp add` (JSON:
`"env": {"KICAD_MCP_TOOLSETS": "all"}`).

### First run

```
> Index KiCAD's libraries  (one-time, ~60 s)
> Create a project at /tmp/test for a voltage divider
```

For a full design session, see
[Example workflow: designing a board](#example-workflow-designing-a-board).

---

## Capabilities

### Tool sets

Every listed tool schema costs context on every turn, so the tools come in
groups and only `core` is registered at startup (12 tools, about 1.2k tokens;
all sets together about 20k, against 36.6k before the rework).

| Set | Contents |
|---|---|
| `core` | `ping`, `create_project`, `set_project`, `get_project_state`, `list_components`, `run_erc`, `run_drc`, `clear_stale_lock`, `help`, `list_toolsets`, `load_toolset`, `unload_toolset` |
| `sch` | schematic edit and query, sheets, buses, fields, nets, annotation, update PCB from schematic |
| `pcb` | board edit (batch `edit_board`) and query, layers, zones, diff pairs, meanders, multi-board, autorouting, RF stitching |
| `rules` | design rules, fab presets, net classes, custom DRC rules |
| `library` | library index and search, symbol and footprint creation |
| `review` | connectivity, circuits, part ratings, power tree, what-if, design diff, lifecycle, assembly, release gate |
| `emc_si` | EMC pre-compliance, return paths, crosstalk, ground coverage, impedance and current calculators, thermal |
| `sim` | SPICE export, ngspice / LTspice runs, subcircuit simulation |
| `datasheets` | datasheet download, extraction cache, specs |
| `sourcing` | distributor availability and keyword search, vendor parts, BOM enrichment and diff, field write-back |
| `fab` | Gerber/drill/pos/BOM/STEP/render exports, fab package, panelization |

- `load_toolset(toolsets=[...])` registers one or more sets (`all` for every set) and sends `notifications/tools/list_changed`, so the client re-lists its tools; the server declares `tools.listChanged`. `unload_toolset` removes sets again; `core` stays. `list_toolsets` shows every set, whether it is loaded, and its tool names.
- A call to a tool whose set is not loaded fails with the set to load.
- Listed descriptions are summaries (at most 500 characters): what the tool does, when to use it, its key arguments. `help(tool)` returns the full documentation — arguments, outputs, method notes, rule IDs — for any tool, loaded or not. Input schemas are listed without pydantic titles and without the `null` branch of optional arguments. Fixed choices are enums and list arguments are arrays (`groups=["ground", "io"]`), so the schema shows the accepted values.
- Budgets are tests (`tests/test_core_toolsets.py`): 500 characters per description, 1600 per listed tool, and a character budget per set.
- `KICAD_MCP_TOOLSETS` picks the startup sets: a comma list, or `all` for clients that defer tool schemas themselves. Default `core`.
- Clients that defer tool schemas (Claude Code's ToolSearch) should fetch tools by exact name (`select:edit_schematic,...`), not by keyword, which pulls in dozens of schemas at once; the server's MCP instructions say so, and `load_toolset` returns the names it added with that hint.
- Sheet-level tools take an optional `sheet` ("root", a `.kicad_sch` file or a root sheet name) and board-level tools an optional `board` ("main" or a `.kicad_pcb` file); without it they use the active sheet / board, and the active one never changes because of the argument.
- The active project, sheet and board survive a server restart (an `/mcp` reconnect): `set_project`, `create_project`, `set_active_sheet` and `set_active_board` record them per working directory in the user cache (`session.json`), and the server restores them at startup when the files still exist.
- Coordinates: a point is `x_mm, y_mm`; anything with two ends (wire, bus, track, box) is `x1_mm, y1_mm, x2_mm, y2_mm`.
- Plain (`def`) tools run in a worker thread, one at a time, so a minutes-long kicad-cli or pcbnew run no longer freezes ping, tool listing or cancellation. Every child process (kicad-cli, KiCAD's Python, ngspice, Freerouting, git) gets its own empty stdin: under stdio the server's stdin is the MCP pipe, and pcbnew inheriting it sat until its timeout (a 55-part `update_pcb_from_schematic` took 260 s instead of 3 s). A test fails on a new `subprocess` call without `stdin=`.

### Safe file writes

- **Round-trip formatting**: a write touches only the nodes it changed. The layout comes from a port of KiCAD's own `Prettify`; all 91 KiCAD 10 demo schematics round-trip byte-identically, so a git diff shows only the edit.
- **Every write to a KiCAD file goes through one guard** (`adapters/safe_write.py`): schematics, boards, libraries and lib tables (`save_tree`), `.kicad_pro` and `.kicad_dru` (`save_text`), and the board rewrites pcbnew and Freerouting do (`guard_external`). It refuses while KiCAD holds the project's `.lck`, backs up to `<file dir>/.backups/<timestamp>_<file>` (newest 10 kept), writes a sibling temp file and swaps it in (as KiCAD does, so a crash never leaves half a file; a swap Windows refuses while an indexer or antivirus holds the new file is retried, then done as a plain copy — datasheet downloads use the same write), re-parses the result and restores the backup if it does not parse. A test fails when new code writes around the guard. An edit that spans several files (annotation, `rename_label`, `refresh_lib_symbols`, `update_symbol_fields`, a new sub-sheet) is all-or-nothing: every lock is checked first, and if one file fails the others get their backups back. Control characters are escaped on write.
- **UTF-8 only**: files are read and written as UTF-8 and kicad-cli / git / ngspice output is decoded as UTF-8, whatever the Windows code page. A KiCAD file with bytes that are not UTF-8 is refused rather than read lossily, so a write cannot corrupt them.
- **Custom DRC rules are edited in place**: adding, replacing or removing a rule changes only that rule's text; comments (`#`), other rules and clauses the server does not model stay byte for byte. A `.kicad_dru` that does not parse is reported, never overwritten.
- **Stale locks**: the refusal says who holds the lock (KiCAD's lock file has only user and host) and whether a KiCAD process is running; `clear_stale_lock` removes it only on explicit request.
- **Strict arguments**: an unknown keyword is an error naming the closest accepted spelling, not silently ignored. Every net argument is named `net`. A few guessed spellings (`x` → `x_mm`, `text` → `net` on labels, …) are renamed and the response lists them under `deprecated_arguments`.

### Schematic capture

- **Symbol placement & connections**: `add_symbol` (with `footprint=` and arbitrary `fields=` in the same call), `add_wire`, `add_label`, `add_power_symbol`, `add_no_connect`, `move_item`, `remove_symbol`, `replace_symbol` (edits the placement in place: user fields, flags, field layout and Footprint are kept, Description and Datasheet come from the new part, a Footprint that does not fit the new part is flagged; wiring, labels and power ports on each mapped pin are collected first and moved once, diagonal wire ends get an L jog, a connection that did not follow is reported; the old part's unused cached definition is dropped), `get_pin_position`, `list_pins` (pins stacked on one point with one name are one entry with `numbers`)
- **Batch editing**: `edit_schematic(ops=[...])` applies many edits in one guarded write, all or nothing (a failing op is named by index and nothing is written; `dry_run=True` applies in memory only). Ops: `symbol`, `wire`, `label`, `power`, `pwr_flag`, `connect`, `no_connect`, `junction`, `text`, `move`, `remove`, `property`, `field`, `autoplace`. Pins are addressed as `"REF.PIN"`, so no coordinates have to be looked up: `connect` draws a 2.54 mm stub out of each pin and ends it in a label or a power port facing away from the body (`style` auto picks a port for ground, stock power nets and nets already drawn with ports); label-style pins within 15 mm of each other are wired together under one label instead (`join=False` keeps a label per pin), and `flag: true` places one PWR_FLAG per net, not per pin; `wire` with `from` / `to` routes a straight or L path that leaves each pin outward and avoids symbol bodies (junction dots added at tees), `pwr_flag` puts the flag beside the connection, on the pin or halfway along its stub, on the side the readability check likes best; a power port on a left / right pin stands upright unless pointing it along the stub collides less; `symbol` and `move` take `mirror` (`x` / `y`); a port or flag dropped mid-wire gets its junction; the PWR_FLAG reminder is judged on the batch's result, for stock and custom ports alike (a net drawn with ports and no `power_out` pin or flag fails ERC `power_pin_not_driven`); a `move` that joins two nets (a wire or label left behind now touching another net) is refused unless `allow_merge: true`; `remove` takes `refs`, a reference `pattern` (`"#PWR*"`), a `label` net (optionally at one point) or `uuids`, and removing a power port or flag takes its stub to the pin; a batch naming an unknown `lib_id` is refused before any op runs, every unknown id listed with a did-you-mean. `check=True` returns the readability counts of the result. The result is compact: the op count, a count per kind, and per-op entries only for what the batch made up itself (auto `#PWR` / `#FLG` references, text uuids, removed items); `verbose=True` returns every entry.
- **Labels**: local and global (`add_label(label_type="global", shape=...)`, KiCAD's `global_label` with its Intersheetrefs property). Text runs in the `orientation` direction; the justification is what KiCAD writes (`left bottom` at 0 / 90°, `right bottom` at 180 / 270°, checked against the KiCAD 10 demos), so a label on a left-facing pin reads away from the part. Hierarchical labels and sheet pins follow the same rule.
- **Move with drag**: `move_item(reference=..., drag=True)` carries the labels, power ports, no-connects and junctions on the symbol's pins; a stub that ends in a label or port moves whole, a longer wire gets a bend that keeps the segment at the pin straight and is never laid on top of another wire. Pins of other parts that touched a moved pin directly are reported (`contacts_broken`).
- **Field autoplace**: `autoplace_fields` puts Reference / Value beside the body on the side with no pins (ties: right, top, left, bottom), horizontal, on grid — where KiCAD put them for R / C at 0 and 180° in every KiCAD 10 demo placed on the grid (test), within one grid step above the part at 90 / 270°. It runs by itself on a rotated `add_symbol` and on a rotation change in `move_item`. A part with pins on every side keeps its library placement unless `force=True`, which then takes the side whose field texts the fewest wires cross.
- **Page and title block**: `set_page(paper="A3", portrait=..., title_block={"title", "date", "rev", "company", "comment1".."comment9"})`; `User` paper takes a width and height.
- **Sheet render**: `render_sheet` returns one schematic page as a PNG image (kicad-cli PDF rasterized with `pypdfium2`), whole page or `region_mm` zoomed, to check a sheet by eye
- **Readability check**: `check_schematic_readability` reports, without moving anything, what makes a sheet hard to read (rules below). Text widths use each character's advance in KiCAD's stroke font, measured from a `kicad-cli` SVG export; confirm by eye.
- **Derived library symbols** (`extends`, e.g. `TLV75733PDBV`, `2N7002`) are flattened like KiCAD's `LIB_SYMBOL::Flatten`, so their pins are placed and wired correctly.
- **Pins landing mid-wire**: KiCAD does not connect a pin to a wire's interior without a junction. `add_symbol` / `add_power_symbol` add the junction and report it (`junctions_added`).
- **Power ports**: any net name works (`add_power_symbol(net="GND_USB")`, built on a stock base symbol with the Value overridden, `base_symbol=` to choose it); the `#PWR` / `#FLG` reference is hidden as in KiCAD. `PWR_FLAG` never names a net.
- **Fields**: `move_item` carries the fields with the symbol (rotation included); `set_field_position`, `set_symbol_property`, `remove_symbol_property`, `get_symbol_properties`, `set_symbol_flags` (dnp / in_bom / on_board / exclude_from_sim)
- **Editing and deletion**: `remove_item` (wire / bus / junction / no-connect by position, or any item by uuid), `add_junction`, `move_item` (a symbol by reference or any item by uuid), `rename_label`, `add_text` / `set_text` / `list_texts` (`kind="labels"`: every net label with net, kind, position, orientation and uuid, `net` filters), `remove_items_in_box` (both endpoints must be inside, so a crossing wire survives), and `remove_symbol(remove_connected_wires=True)` to drop the stubs left on the removed pins (followed through bends to the rest of the net; a wire that is the only link to another pin or a label stays)
- **Connectivity model**: `list_sch_nets` (`compact=True`: pins as `REF.PIN`, single-pin `unconnected-*` nets only counted), `trace_net`, `get_pin_net` (part pins only, power ports counted; `count_only=True`), `find_dangling` — the same connectivity KiCAD uses (pin positions for every rotation × mirror, mirror applied after the rotation as KiCAD composes it, checked against `kicad-cli`; net names from power symbols with a `power_in` pin, labels, hierarchy). Unlabelled nets get KiCAD's default names (`Net-(Q3-D)`, `unconnected-(U5-NC-Pad4)`: `-Pad<n>` when the pin has no name, is unconnected, or shares its name with another pin of the part; of the net's pins KiCAD's choice names it — a named pin beats a `-Pad` name, then the smaller name wins, and a named pin of a multi-unit part carries the unit letter, `Net-(U1A-G)`), every name checked against `kicad-cli sch export netlist`; `find_dangling` skips pins of type `no_connect`, as ERC does, and reports a lone wire end even when the wire's other end is connected (ERC's `unconnected_wire_endpoint`).
- **Grid discipline**: every placement snaps to KiCAD's 1.27 mm grid unless you pass `snap_to_grid=False`, so endpoints actually connect
- **Hierarchical sheets**: `add_sheet`, `set_active_sheet` (the root sheet by its file name too), `get_active_sheet`, `add_hierarchical_label`, `add_sheet_pin`, `list_sheets`; `list_components(sheet=…)` lists one sheet without switching
- **Buses**: `add_bus`, `add_bus_entry`, `add_bus_alias` — visual grouping of address/data lines
- **Annotation**: `annotate_schematic` — auto-numbers `R?` → `R1, R2, ...` across the whole hierarchy, nested sheets included. A sheet used twice gets one reference per instance (KiCAD's `(instances … (path … (reference …)))`), the next free number counts every instance's reference, and the units of one multi-unit part share a number (U1A / U1B). A locked project is refused before any sheet is written.
- **Custom symbols**: `create_symbol` with full pin specifications and arbitrary fields; `refresh_lib_symbols` updates a sheet's cached library symbols, `list_lib_symbols` lists them

| Rule (`check_schematic_readability`) | Severity | Meaning |
|---|---|---|
| `SR-OVERLAP` | warning | Two texts (touching corners count), a text and another part or a power port / flag, two parts, or two ports stacked on one spot overlap |
| `SR-TEXT-ON-BODY` | warning | A field spills over its own symbol (text wholly inside the body, such as an R value, is deliberate and not reported) |
| `SR-LABEL-INTO-BODY`, `SR-PORT-INTO-BODY` | warning | A label's text or a power port's graphic runs into a part |
| `SR-FRAME`, `SR-TITLE-BLOCK` | warning | On or past the sheet border (its 2 mm zone-letter band included) / on the title block |
| `SR-WIRE-THROUGH-BODY` | warning | A wire crosses a part it does not connect to |
| `SR-OFF-GRID` | warning | A part's origin is off the 1.27 mm grid |
| `SR-TEXT-OVER-WIRE` | info | A wire runs through a field or note |
| `SR-SHORT-LABEL-NET` | info | Two or three local labels of one net within 15 mm: a wire shows it better |
| `SR-CROSSINGS` | info | More than four wire crossings: labels read better than long crossing wires |

### Library management

- **KiCAD's libraries indexed** — `index_libraries` (project tables and KiCAD's global `sym-lib-table` / `fp-lib-table`, with `${VAR}` expansion), `list_libraries`, `search_symbol`, `search_footprint`, `get_symbol_details` (each pin with the body side it sits on, to plan a placement; pins sharing name, type, shape and side grouped into one entry with `numbers` and `at` — one point when stacked, else one per number, so USB-C D+ A6 / B6 keep their places — unnamed pins never; `group_pins=False` lists each with its library position), `check_lib_ids` (many symbol / footprint ids in one call: exists, pin count, default footprint, did-you-mean). Search ranks exact name, then prefix, then substring, then fuzzy matches; each hit says how it matched (`_match`: `name`, `family` when only a shorter prefix of a part number the libraries lack matched — same series, not that part — or `text` for description words); a vendor prefix is dropped when nothing else matches (`SN74AHCT1G125` finds `74AHCT1G125`), and in a several-word query a word that names a symbol still matches by name (`D_TVS unidirectional`); the result is `{"hits": [...]}` (lib_id, description, pin count, default footprint, score; `verbose=True` the whole entry) plus a `note` when no hit matches by name or family, naming `Device:D_Zener` (pin 1 = K) for a unidirectional TVS part number (SMAJ / SMBJ / SMCJ / SMF ... `A`) and `Device:D_TVS` for a bidirectional one (`CA`).
- **External parts**: `find_or_fetch_symbol`, `import_vendor_zip` (SnapEDA / Ultra Librarian), `list_vendor_parts`
- **Custom footprints**: `create_footprint` with auto-courtyard and silk outline

### Sourcing and datasheets

- **Availability**: `check_availability` against Mouser, DigiKey, TME, Farnell and JLCPCB (opt-in source: LCSC number, stock, USD price breaks, basic / preferred / extended library type, no key needed). Every hit carries the distributor's own order number as `sku` (Mouser part number, DigiKey product number, TME symbol, Farnell order code, LCSC number), all its `price_breaks` (`[{qty, unit_price}]`) and `unit_price_at_qty` for the requested `qty`
- **Order quantity and SKU choice**: a distributor lists one MPN under several SKUs (reel, cut tape, re-reel); the tools pick the exact-MPN SKU that is in stock for the order at the lowest line cost. `buy_qty` raises the needed quantity to the SKU's minimum order (Farnell `translatedMinimumOrderQuality`, Mouser `Min`, else the first price break) and order multiple (Mouser `Mult`); `unit_price_at_qty` and `line_total` are priced at `buy_qty`. `enrich_bom_with_sourcing` writes a `<prefix>_buy_qty` column and totals at those quantities
- **Keyword search**: `search_distributors` finds parts that have no MPN yet (`22uF 10V 0805 X7R`) at the chosen sources, cheapest order (in stock, at `buy_qty`) first
- **Mouser rate limit**: calls are throttled to Mouser's ~30 per minute; a refusal comes back as a clean `TooManyRequests` error
- **Single datasheet**: `fetch_datasheet` tries the given URL and each source's datasheet link in order and keeps the first body that really is a PDF (bot-protection HTML is rejected; TLS verification is never disabled); every attempt is reported. A Datasheet URL comes from whatever schematic is open, so downloads are http(s) only, go only to public addresses (loopback, private, link-local and reserved hosts are refused, on every redirect; `KICAD_MCP_ALLOW_PRIVATE_DOWNLOADS=1` allows an intranet server), follow at most 5 redirects and stop at 60 MB or 120 s
- **Encrypted datasheets**: AES-encrypted PDFs (Panasonic, JST) are read through `pypdf[crypto]`
- **Project datasheet folder**: `sync_datasheets` gives every schematic MPN its PDF in `datasheets/` and records the mapping in `datasheets/manifest.kicad-claude.json`. Existing vendor-named files (`8471fd.pdf`) are matched through the symbol's Datasheet URL, the PDF's own text (MPN or family stem) or the file name; the rest is downloaded. The folder is `<project>/datasheets`, or the nearest parent `datasheets/` up to the git root (one shared folder for a multi-board repo). Reports unmapped parts and orphan PDFs.
- **Structured specs, extracted by the calling agent**: `get_datasheet_extraction_task` hands over the PDF path, the text of the pages holding the tables (chosen from the datasheet's table of contents — printed page numbers moved to the PDF page that names the section — then the best page per section: pinout, absolute maximum, recommended, electrical, thermal, application; each suggested page carries its reason), the part's schematic pins and nets, and the schema; `save_datasheet_extraction` validates the record and caches it in `datasheets/specs/<MPN>.json`. Every value carries its PDF page and SI unit; the server checks each number against the cited page's text with its sign, decimal point and unit (0.075 V must read `75 mV` or `0.075 V`; another scale only with that scale's unit, a bare table cell only at base scale; −40 needs its minus; `±15 V` satisfies only a −15 / 15 pair; Unicode minus and dashes, thousands separators and line-wrap hyphenation are tolerated) and warns (`unverified`) when it is not there. The extraction is bound to the PDF it was read from: the task returns the PDF's SHA-256, `save_datasheet_extraction` requires it back and refuses the save when the file changed in between; a saved record goes `stale` when its PDF changes later. `get_datasheet_specs` returns one record or the coverage. Saved and read records carry a `quality` score 0–100 with what is missing — pin table against the symbol's pin count (from the last netlist export), voltage ratings, electrical values, thermal, and the keys the analyzers read for the part's category — and `DQ-LOW` (info) below 50. A manifest entry that names a file outside `datasheets/` (a crafted manifest in a cloned repo) is refused. No value is taken from general knowledge. Pin behaviour the DC model uses goes in `pin_models` (clamp, trip current, bias current, input resistance, keyed by pin name). Graphs go in `curves` (points read off the plot, with page; e.g. a capacitor's `C_bias`, capacitance vs DC bias). The schema names the keys the analyzers read: capacitor `C`, `ESR`, `ESL`, rated `V`, `C_bias`; inductor `L`, `DCR`, `Isat`, `Irms*`; common-mode choke `Z_cm`, `DCR`, `I_rated`; LED `VF`, `IF`; constant-current regulator `I_reg`, `V_min`; connector `I_rated` (per contact) / `I_rated_total` (a rail's contacts together); `thermal.rth_ja` / `tj_max`; op-amp `GBW`; crystal `CL`.
- **BOM enrichment**: `enrich_bom_with_sourcing` queries the chosen sources per unique value and appends MPN / stock / price / URL / order-number columns (JLCPCB adds `jlc_lcsc`, `jlc_type`). Each line is priced at its quantity × `boards` from the price breaks (`<prefix>_qty_price`, `<prefix>_line_total`), and the result carries per-source `totals` for the batch
- API keys never reach the log: request URLs are not logged and key-like query values are redacted.

### PCB layout

- **2 to 32 copper layers** with auto-generated stackup (`set_layer_count`)
- **Footprint placement**: `add_footprint`, `move_footprint`, `place_footprints_grid`, `remove_footprint`
- **Batch board editing**: `edit_board(ops=[...])` moves footprints and adds tracks, vias and zones or removes items in one guarded write, all or nothing (`dry_run`); pads are addressed as `"REF.PAD"`, and a track or via between pads takes their net. `move` also places parts relative to another: `"near": "U1.2", "side": "left"` puts the part (or several `refs`, stacked along that side) beside the anchor's courtyard, `gap_mm` apart, centred on the anchor or its pad. `zone_set` changes a pour in place (pad connection, priority, clearance, thermal gap / spoke, minimum width) by uuid, name or net; `text` adds board text (returns its uuid) or edits one by uuid, and `remove` takes a text uuid
- **Routing**: `add_track`, `add_via` (on a net by name; an integer index only on a board that still has a net table), `add_via_array`, `add_meander` (length tuning); `remove_board_items` removes one track / arc / via / zone by uuid (DRC reports carry them) or rips up a net, optionally on one layer, with `dry_run`
- **Copper pours**: `add_zone`, `add_ground_plane` (auto-extracts board outline, rounded corners included); `pad_connection` thermal / solid / thru_hole_only / none, thermal gap and spoke width, and `priority` for overlapping pours
- **Mechanical**: `add_mounting_hole` (M2/M2.5/M3/M4/M5), `add_fiducial`, `add_silk_text` (returns its uuid; text on a back layer is mirrored, as DRC requires)
- **Footprint fields**: `set_footprint_field` shows / hides, moves (board position), rotates, resizes (thickness follows, or `thickness_mm`) or re-layers a placed footprint's Reference, Value or user field, for one or many parts. `tidy_footprint_fields` is the silkscreen pass KiCAD lacks: each Reference on silkscreen moves to the first free spot around its courtyard (above, below, right, left, the corners, then further out), horizontal, clear of pads on that side, silkscreen graphics, the other references and the board edge; Values on silkscreen are hidden. Parts with no free spot are listed (`hide_unplaced=True` hides their reference); `size_mm` resizes first, a reference below the project's `min_text_height` / `min_text_thickness` is raised to them (`raised_to_minimum`), `dry_run` reports only; `moved` lists references (`verbose=True`: positions). Text extents are a stroke-font estimate, so confirm with `run_drc`.
- **RF specials**: `add_rf_microstrip` (target Z₀), `add_ground_stitching`
- **Board render**: `render_board` returns the board as a PNG image (kicad-cli PDF in KiCAD's colours, rasterized with `pypdfium2`): chosen `layers` (default copper, silkscreen, Edge.Cuts; add F.Fab for values), cut to the outline + 1 mm or a `region_mm` zoom, `bottom=True` mirrored to view from below; the side looked at is painted last so its copper is not hidden by the far side's pour
- **Inspection**: `list_footprints` (with the courtyard and body (F.Fab) boxes in board coordinates, `courtyard_is_box: false` for a courtyard that is not a rectangle — a module's T-shaped antenna keep-out leaves free notches beside the body that the box covers — and for a connector `edge_side`, the board side its mating face points to, from the footprint's "PCB Edge" line or its body overhang; rotation 0 … 360, also for boards pcbnew saved as −180 … 180), `list_pads` (with pad shape, size and the pad's box on the board axes), `get_pad_position`, `list_nets`, `list_unrouted` (with `summary` / per-net roll-up; each filled pour piece is its own island, a pad joins a pour only where the fill overlaps its outline, copper reaching no pad is listed as `islands`, and pads of one net whose copper overlaps — USB-C's stacked A4 / B9 — are one node, so the total matches KiCAD's DRC), `net_route_status`, `compute_trace_length`, `list_diff_pair_candidates`, `validate_diff_pair_length_match`
- **Rotation**: rotating a footprint rotates its pads too (pad angles in `.kicad_pcb` are absolute)
- **Sync**: `update_pcb_from_schematic` adds, updates and removes footprints and applies pad nets through KiCAD's own `pcbnew` from a kicadsexpr netlist (escaped net names such as `{slash}` survive); it saves the board only when something changed, `scope="fields"` updates only fields (fast), and the timeout scales with the design; the symbol's `in_bom` / `dnp` / `exclude_from_sim` flags become the footprint's attributes and `on_board=no` symbols stay off the board, as in KiCAD's own update; a footprint's own zones (antenna keep-outs) are placed in board coordinates and follow moves and rotations; the result is compact (count placed, count per field) unless `verbose=True`
- **Coordinates**: schematic and PCB tools both take KiCAD-native coordinates — mm, Y down, the numbers the GUI shows

### Autorouting

- **Freerouting integration** — `autoroute_pcb` runs the full pipeline (DSN export → routing → SES import) with timeout; returns the passes run, the final score, unrouted connections and violations from Freerouting's log, and `after_import`: zones refilled, KiCAD DRC's unconnected count and the per-net ratsnest that is really left (Freerouting does not see pours split by its tracks, nor route a pad to an unreachable pour of its net). KiCAD's DSN export turns every pour into a plane, so `pour_guard` lists, before routing, the pads of each pour net that lie outside all its outlines (a pad above a pour on another layer is fine: Freerouting drops a via); `route_pour_nets=True` exports the DSN from a copy without the pours, so those nets are routed with tracks, and the pours stay on the board, refilled after the import; `export_dsn` / `import_ses` run the two halves by hand (route the DSN in Freerouting's GUI, then apply the session)
- **Diff pair coupling** — auto-detected by net naming (`_P/_N`, `+/-`, `DP/DM`); routed coupled when net class has `diff_pair_width`/`gap` set

### Validation

- **ERC and DRC** — `run_erc`, `run_drc` shell out to `kicad-cli` and parse the JSON (every sheet's violations); `run_drc(summary=True)` returns counts by type and severity, `max_violations=N` caps the lists
- **Custom DRC rules** — `add_drc_rule` writes `.kicad_dru` with constraint types (clearance, track_width, length, skew, diff_pair_gap, …) and conditions in KiCAD's expression language; `list_drc_rules`, `remove_drc_rule`, `clear_drc_rules`
- **Net classes + fab presets** — `apply_fab_preset` with `jlcpcb_2l_default`, `jlcpcb_2l_advanced`, `jlcpcb_4l`, `aisler_2l`, `aisler_4l`, `pcbway_default`, `oshpark_4l`, `permissive_prototype` (`list_fab_presets`). When the preset conflicts with the project's own rules it changes nothing and returns the comparison with `decision_required` (without a conflict only the rules applied); the caller picks `mode="project"`, `"preset"` or `"stricter"` (per-rule stricter value wins); `dry_run=True` previews. `add_net_class`, `add_diff_pair_class`, `assign_net_class` (one `net_pattern` or several `patterns`, with the `matched_nets` of each and a warning for a pattern that matches nothing; both list `tight_footprints`: parts on the class's nets whose own pad spacing is below its clearance, which DRC would flag), `list_net_classes`, `remove_net_class`; board minimums (clearance, track, via, hole) with `set_design_rules`

### Design review (from KiCAD's own netlist and the parts' datasheets)

Both tools export the whole hierarchy with `kicad-cli sch export netlist`, so the connectivity is exactly KiCAD's. They are pin-name and pin-type heuristics: every finding says what it saw, and should be confirmed against the schematic and datasheet.

- **Untyped vendor symbols**: an IC whose pins are all `passive` (common in SnapEDA / Ultra Librarian symbols) gets its supply and ground pins (VCC, VDD, AVDD, VIN, GND, VSS, …) typed `power_in` by name, so the power, decoupling and EMC checks see them; the component carries `pin_types: "inferred"`. Typed symbols are left as drawn.
- **Unknown supplies stay unknown**: a net with a supply or connector pin and no known voltage is not solved from resistors to ground alone (an EN divider would otherwise put a USB-PD rail behind a load switch at a few mV); pass `rail_voltages` for it.
- **Component values** are read as KiCAD's simulator reads them: `4k7`, `2.2Meg`, `10 µF` / `10μF` (micro sign or Greek mu, unit after a space), decimal comma (`4,7k`), the first field of `0.1uF, 50V`, digit groups (`78 182`). `1,000` (decimal comma or thousands separator) is left unparsed, not guessed.
- **`analyze_power_tree`** — finds each regulator (one record per channel on multi-channel parts), its topology, input and output rail, EN state (tied to input, divider, driven by another part's pin = sequencing, floating …), power-good pull-up, feedback divider (values, ratio), SET resistor, switch-node inductor and input / output capacitors. Builds the rail tree through series L / ferrite / 0 Ω / fuse links and OR-ing diodes (anode to cathode; zeners and TVS are no link), so both inputs of a diode OR (buck output and USB VBUS into +5V) are rails (DNP marked); a rail no regulator outputs lists what feeds it (`fed_via`: OR-ing diodes, and a fuse / ferrite / 0 Ω from the connector side), and a diode into a rail drawn under another root shows as a `joins` entry. The power tree's DC model runs without switch states (a load switch's gate at its pull-up); per-state values come from `review_connectivity`. Computes each Vout from the datasheet specs — FB divider with the right-polarity Vref, ANY-OUT pin strapping, or SET current × R —, from a caller `vref`, or for a fixed-output part from its symbol description ("Fixed Output 3.3V"), and carries rail voltages through the links. With those voltages it checks VIN against absolute maximum and recommended range, VOUT range, LDO headroom against dropout, and the EN pin voltage (divider / pull-up, power-good released) against the EN rating and turn-on threshold. Unregulated inputs (a USB-PD contract) come from `rail_voltages={"/VBUS": 20}`. Nets off the rails (EN dividers, multi-branch networks) come from the DC operating-point model below, so an EN pin held by an internal clamp is checked at the clamped voltage; solved nets are returned as `net_voltages`.
- **`review_connectivity`** — checks (all on by default, choose with `checks=`): IC power pins and how each rail is sourced; decoupling per IC supply net; open-drain outputs without pull-up; I2C buses (pull-ups, address strapping, fixed datasheet addresses); symbol pins against the datasheet pin table; every pin and pin pair (VDS, VGS, TVS standoff) against its absolute maximum at the net's voltage; capacitor DC voltage against its rating — the fitted MPN's extracted datasheet when there is one, else the `Voltage` field. Derating follows `derating_profile` (`commercial` default, `industrial`, `automotive`, `military`; policy values, not part data): the capacitor fraction per dielectric family read from the symbol (ceramic / electrolytic / tantalum / unknown — commercial 50 / 80 / 80 / 50 %, military 40 / 60 / 50 / 40 %; `cap_derating` overrides it with one fraction), and IC pins beyond 90 / 85 / 85 / 80 % of their absolute maximum (`CR-PIN-DERATE`). The pin and capacitor checks run on the DC operating-point model in each switch scenario (`switch_states`: `auto` by default — every combination up to `max_switch_combos`, else all-on / all-off —, `all`, `both`, `on`, `off`, `none`; `switch_overrides={"Q3": "off"}`).
- **DC operating-point model** (`analyze_power_tree`, `review_connectivity`) — on top of the rail voltages: every fitted resistor network solved by nodal analysis (0 Ω, inductors and ferrites are shorts); datasheet pin models (`pin_models` in the spec record: internal clamp, trip current such as an OVLO threshold, bias current, input resistance; `regulator.en_pullup_current` is applied to EN pins); zeners that clamp (VZ max from the spec cache, else the part number — 1N52xx / MMSZ52xx table, BZX / BZT voltage code — else a voltage in the value; a zener with none is named in the notes), solved iteratively; transistor switches — `on` joins drain and source and the gate driver (an IC output, directly or through a series resistor) pulls to the on level, limited to the pin's datasheet drive current (`pin_models` `i_sink` / `i_source`; above it the pin becomes a current source and DC-DRIVE warns); `off` passes only the body diode (VSD from the spec cache, else no drop) and leaves the far side at 0 V — unless a resistor chain ties that side to a known voltage above it or to a supply rail (a pull-up on an off FET's drain), which the network then solves. A switcher's output held by its regulator is not solved from its feedback divider. A driver's high level is its `v_oh`, else its internal regulator's `v_out` (e.g. VDDD), else an unambiguous supply. `review_connectivity` runs every on / off combination of the switches up to `max_switch_combos` (default 16) (above that: all on, all off and each switch flipped alone) and reports each pin's worst case (`pin_abs_max_worst`, `cap_voltage_worst`); DC-NODATA lists what the model ran without (drive ratings, body-diode VSD, P / N type, zener VZ); findings that occur only in some states say which (`[when Q1 on]`). Static DC: capacitors open, no switching transients.
- **`review_part_ratings`** — parts against their datasheet ratings with a per-rail current model. `loads={"+5VD": 0.5}` (amps per rail) is carried up the power tree: series links unchanged, linear regulators Iin = Iout, switchers by power with `efficiency` per regulator (others use `default_efficiency`, reported as assumed), and from each root rail through the series path (inductors, common-mode choke winding, ≤ 1 Ω resistors, FET dies, series diodes, fuses) to the connector that brings power in (two or more contacts, a supply-named contact, or a barrel / DC jack). A load on a net the power tree does not hold — behind a load switch or an output fuse — is carried through its series path to the nearest rail, and the FETs, fuses and diodes on that path are checked at that current (`switched_loads`; `RT-LOAD-PATH` when no path exists). Inductor ripple from L, `fsw` (caller or datasheet), Vin, Vout for buck, boost, inverting buck-boost and two-inductor Ćuk / SEPIC / Zeta stages (ideal CCM duty; the switch peak is the sum of the inductor peaks, checked against the switch current limit), with a DCM flag and the DCM peak √(2·Iavg·ΔI) for single-inductor stages; two-inductor stages use the summed current (L1 ∥ L2): in DCM each winding's ripple shrinks with the on-time and the switch peak is √(2·ΣI·ΣΔI); a coupled inductor (one 4-pin part, windings 1-2 / 3-4) has half the ripple of two separate ones; a forced-PWM part (`regulator.light_load: fccm`) stays CCM. Efficiency comes from the caller, else the datasheet's `curves.efficiency*` (closest VIN / VOUT), else the default. A channel whose power enters through another pin (`regulator.channels.N.input_pin`, LT8471 C2) and pins driven at another pin + offset (`regulator.internal_outputs`, LT8471 SHOUT = C2 + Skyhook boost) come from the spec cache. Switcher Tj uses the converter loss minus what the spec cache can price outside the IC (inductor DCR at RMS current, rectifier VF × average current); parts without DCR / VF stay charged to the IC and are named. An efficiency curve far from the stage's VIN / VOUT is flagged as an estimate. `vref` (as in `analyze_power_tree`) sets an adjustable regulator's Vout when no datasheet Vref is extracted, so its input current reaches the upstream rails. Junction temperature Ta + rth_ja·P (`ambient_c`, default 40 °C, stated as assumed) for regulators (channels of one IC summed), FETs (I²·RDS(on) per die), series diodes and LED current regulators; rth_ja is the datasheet's test board. Without an extracted rth_ja the package-typical value is used as a labelled estimate (`rth_source: "package_typical (assumed)"`; JEDEC still-air figure by package family from the footprint name — SOT-23, SOIC-8, QFN by body size, TO-252, …; table from kicad-happy), and a Tj finding resting on it is at most a warning; an unknown package gets no estimate. Without an extracted Tj max, an estimate above 125 °C is still a warning. This table is the only value the server takes from general knowledge. `rth_ja={"U2": 60.3}` (C/W per ref, e.g. the package's thermal table or a measured board) wins over both (`rth_source: "caller"`). LED strings: current from the series R and VF (VF at the operating current from `curves.VF` when extracted), or a CCR's `I_reg` and its headroom. `derating_profile` sets the Tj margin (25 / 25 / 30 / 40 °C, `tj_margin_c` overrides) and the resistor power fraction (50 %): `RT-R-P` checks each resistor's dissipation against its rating from the extracted datasheet or the `Power` field (never guessed from the package). Fuses and PTCs in a rail's series path are checked against the rail current (`I_hold` / `I_trip` / `I_rated` from the spec cache, else the `Current` field). `power_budget` gives per rail voltage, current and power, per regulator Iin / Iout / loss, and the input current and total input power at the root rails; `sleep_current` the standby draw per rail — regulators' Iq from the spec cache (I_shdn for regulators set `off` in `sleep_states`, whose rails then sit at 0 V) and every resistor current leaving a rail in the sleep DC operating point (dividers, bleeders, pull-ups held low; `sleep_states` also sets switches) — carried up to the input, missing Iq listed. Over-specified parts are a cost hint (`RT-OVERSPEC`, info). `dissipation` lists every part that heats, largest first, with its method: the regulators, FETs, diodes and CCRs above, and every fitted resistor — I²R for those in a rail's series path (shunts, ≤ 1 Ω links), ΔV²/R from the DC operating point for the rest (dividers, pull-ups).
- **`review_circuits`** — circuit detectors in eight groups (`groups=`), checked against eleven of KiCAD's bundled demo designs (no errors; acceptance test): **power** — USB Type-C CC termination (Rd 5.1 k per CC pin, Rp value vs its rail, controller pin, CC1 + CC2 on one resistor), D+ / D- of both plug orientations joined, clamps on connector supply nets, ground pins on power connectors, ESD clamps on external connector signal nets (supply nets — by name, or one fuse / ferrite away from one — are not signals; `internal_connectors` excludes board-to-board and test connectors), reverse-polarity protection of connector supplies that reach ICs (series diode, drain-upstream FET, ideal-diode IC, or a shunt diode; the topology is reported), inrush per supply entry (capacitance up to the first regulator, through diodes only anode to cathode, hot-plug peak V / `source_ohm` against `inrush_limit_a`, the connector rating and fuses in the path; USB VBUS above 10 µF) and each regulator's soft-start charge current C·Vout / `t_ss` from the spec cache, enable loops and self-enable, reset nets, FB divider impedance and FB bias-current error, high-side P-FET load switches (the gate pull-up, V_GS zener, driver FET and its command net, the command's pull-down that holds it off while the MCU pin is high-impedance, a drain-gate Miller capacitor direct or through a steering diode; V_GS on and off from the DC model at the given rail voltages, against the V_GS the datasheet rates R_DS(on) at and V_GSS — run it at the lowest input too), data lines that leave on the same connector as a switched rail (the driving IC needs an enable that follows the switch command and rests disabled; 74xG125 / 126 with unnamed pins are known) and fuse selectivity (a branch fuse behind another one needs the smaller melting I²t, `electrical.I2t` in the spec cache); **analog** — op-amp inputs with no DC path, input coupling high-pass corner vs `audio_f_low_hz`, output coupling caps to a connector without a bleed resistor, unused op-amp channels, converter reference / VCOM caps, analog supply decoupling, split converter grounds with no join, ADC inputs without RC, jacks driven straight from an output, level shifters; **digital** — inputs with nothing driving them (pins the datasheet lets float — ANY-OUT, "may be left open" — are skipped; a symbol input the datasheet calls an output is `DG-PINTYPE`), boot / mode straps, I2C pull-ups stronger than the mode's sink (3 mA; 20 mA in Fast-mode Plus) and I2C rise time per line (0.8473 · pull-up ∥ · bus C against `i2c_mode` `standard` / `fast` / `fast_plus`; C is the devices' extracted `C_in` plus `i2c_bus_capacitance_pf={net: pF}`), SPI chip selects shared or without pull-up, driver vs receiver supply voltage (also UART TX → RX through series resistors), branched clocks without a series resistor, USB data ESD, SWD pins brought out; **misc** — LEDs without a current limit, LED-driver set pins, display charge-pump / VCOMH caps, RTC backup pin, buttons without pull-up, switch matrices without diodes; **interfaces** — Ethernet magnetics, Bob Smith termination and centre taps, HDMI / DVI ESD, DDC pull-ups, HPD and +5V protection, LVDS receiver termination, CAN bus terminations (120 Ω or split 2 × 60 Ω + cap, counted per CANH / CANL pair through series parts and a common-mode choke) and ESD, flash WP / HOLD and DDR VREF, radio-module antenna path; **power_conversion** — charger set resistor and thermistor, BMS cell-sense RC and balancing, cell-protection sense, opto + shunt-reference feedback (bias, compensation), series supercap balancing, isolator grounds bridged, Y-caps, per-side decoupling; **drives** — motor-driver bootstrap, sense resistor and VM caps, freewheel diodes on low-side switches with inductive loads, thermocouple filtering, RTD reference resistor and its tolerance, addressable LED chains (decoupling, data level vs 0.7 VDD, series resistor); **rf** — RF pins reaching a connector without a DC block, matching networks (pi / T / L, all-DNP pads) and the standards the detected circuits point to (CISPR 32 / FCC 15B, RED / FCC 15C, IEEE 802.3 isolation, IEC 62133 / UN 38.3, IEC 62368-1, USB-IF, isolator and drive standards).

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
| `CR-PIN-DERATE` | warning | A pin within its absolute maximum but beyond the derating profile's fraction of it |
| `CR-CAP-V` / `CR-CAP-DERATE`, `CR-CAP-FIELD` | error / warning | Capacitor DC voltage above its rating / above the derating fraction (profile, by dielectric family); `Voltage` field differs from the MPN's datasheet |
| `DC-CLAMP`, `DC-TRIP` | info, warning | A pin held at its internal clamp (current into it, and the source voltage at which its trip current is reached) / at or above the trip current (e.g. OVLO) |
| `DC-ZENER-P`, `DC-DRIVE` | info / warning / error, info | A conducting zener's current and power vs Ptot / a gate driver's sink or source current |
| `RT-L-ISAT`, `RT-L-IRMS` | error (warning above 80 % Isat), warning | Inductor peak current vs Isat, average vs the lowest Irms |
| `RT-CMC-I`, `RT-CONN-I` | error | Common-mode choke current vs I_rated; connector current per contact / per rail group |
| `RT-REG-IOUT`, `RT-REG-ILIM` | warning / error | Regulator output vs recommended IOUT / current limit; buck inductor peak vs the switch limit |
| `RT-TJ` | error / warning | Junction temperature above Tj max / within `tj_margin_c` of it (warning only when rth_ja is the package-typical estimate) |
| `RT-LOAD-PATH` | info | A load given on a net that reaches no rail of the power tree through series parts: not modelled |
| `RT-FET-ID`, `RT-D-IF`, `RT-LED` | error / warning | FET / series diode / LED current vs rating; CCR without headroom; LED with no limit |
| `RT-FUSE` | error | Fuse / PTC hold (or rated) current below the rail current through it, or trip current at or below it |
| `RT-OVERSPEC` | info | Cost / size hint: capacitor rated ≥ 4× its DC voltage (≥ 50 V rating), fuse ≥ 5× its load, regulator IOUT ≥ 10× its load |
| `RT-R-P` | error / warning | Resistor dissipation above its power rating (datasheet or `Power` field) / above the profile's derating |
| `RT-MODEL`, `RT-NODATA` | info | Regulator voltages unknown (current not carried up) / a part carries current but has no extracted rating |
| `PW-USBC-CC`, `PW-USBC-DATA` | error / warning | Type-C CC termination; D+ / D- of the two orientations not joined |
| `PW-VBUS-TVS`, `PW-CONN-GND`, `PW-ESD` | warning, info | Supply entry without clamp / without a ground pin; external signal nets without ESD clamp |
| `SW-OFF-DEFAULT`, `SW-RESET` | error, warning | High-side switch with no gate pull-up (or not off with its driver off) / its command net has no pull-down, so it can turn on during reset |
| `SW-VGS-ON`, `SW-VGS-MAX`, `SW-ZENER` | warning, error / warning, warning | On-state V_GS below the R_DS(on) rating (4.5 V without a spec) / above V_GSS (20 V without a spec) / gate zener not below V_GSS |
| `SW-SOFTSTART` | info | No drain-gate capacitor: turn-on dV/dt not limited |
| `SW-BACKPOWER`, `SW-OE`, `SW-OE-DEFAULT`, `SW-DATA-PULL` | warning, info | A data line on the switched connector driven with no enable / enable not following the switch command / enable with no resistor to its disabled level / line with no pull-down |
| `SW-FUSE-SEL` | warning / info | Branch fuse's melting I²t (or rating) not below the upstream fuse's / values missing |
| `PW-INRUSH` | warning / info | Hot-plug peak (V / `source_ohm`) into the capacitance before the first regulator above `inrush_limit_a`, the connector rating or a fuse in the path; USB VBUS above 10 µF (USB 2.0 7.2.4.1); soft-start charge current above the limit / peak not estimated |
| `PW-REVPOL` | warning | Connector supply reaching ICs with no series diode, drain-upstream FET or ideal-diode IC (USB and internal connectors skipped) |
| `PW-SEQ-LOOP`, `PW-SEQ-SELF`, `PW-RESET` | error, warning / info | Enable loop / regulator enabled by a part it powers; reset net pull-up and filter cap |
| `PW-FB-IB`, `PW-FB-Z` | warning, info | FB bias current × R_top above 0.5 % of Vout; divider impedance above `fb_z_max_ohm` |
| `AU-BIAS`, `AU-HPF`, `AU-BLEED`, `AU-UNUSED` | error, warning | Op-amp input without DC path; coupling high-pass in the audio band; no bleed resistor; unused channel floating |
| `AD-VREF`, `AD-SUPPLY`, `AD-GND`, `AD-AAF` | warning, info | Converter reference / analog supply without cap; split grounds not joined; inputs without RC |
| `HP-JACK`, `LS-SAME`, `LS-OE` | warning, info | Jack driven directly from an output; level shifter on one rail / OE not driven |
| `DG-FLOAT`, `DG-PINTYPE`, `DG-BOOT` | error / warning, info | Input with no driver or unconnected; symbol pin type vs datasheet; boot strap without resistor |
| `DG-UART-LEVEL` | warning | UART TX into another part's RX through series resistors across supply domains (direct connections are `DG-LEVEL`) |
| `DG-I2C-RISE` | warning / info | I2C rise time 0.8473·R·C above the `i2c_mode` limit (1000 / 300 / 120 ns, NXP UM10204) / not computed (no capacitance known) |
| `DG-I2C-RPU`, `DG-SPI-CS`, `DG-LEVEL`, `DG-CLK-TERM`, `DG-USB-ESD`, `DG-SWD` | error / warning / info | I2C pull-up too strong; SPI CS shared / no pull-up; supply mismatch driver → receiver; clock fan-out without series R; USB data without ESD; debug pins not reachable |
| `ETH-*`, `HDMI-*`, `LVDS-TERM`, `MEM-*`, `WL-*` | error / warning / info | Interfaces: magnetics, termination, ESD, pull-ups, flash / DDR straps, antenna path |
| `CAN-TERM`, `CAN-ESD` | error / warning / info | CAN bus: more than two terminations / none on a node with a connector (info with a DNP option) or a wrong value; bus at a connector without a clamp |
| `CHG-*`, `BMS-*`, `PROT-CS`, `TF-*`, `SC-BAL`, `ISO-*` | error / warning / info | Power conversion: charger, BMS, isolated feedback, supercaps, isolation barrier |
| `MD-*`, `TC-FILTER`, `RTD-RREF`, `ALED-*` | error / warning / info | Motor drivers, temperature sensing, addressable LEDs |
| `RF-DCBLOCK`, `RF-MATCH`, `RF-MATCH-OPEN`, `CERT-*` | warning / info | RF paths and matching; certification hints |
| `LE-NORES`, `LE-ISET`, `DS-CAP`, `RTC-VBAT`, `KB-PULL`, `KB-GHOST` | warning / error / info | LED without limit; LED driver set pin open; display pump caps missing; RTC backup pin; button without pull-up; key matrix without diodes |

Parts of the rule vocabulary (pin-name sets, non-regulator exclusions) and the scope of the circuit detectors follow kicad-happy (MIT, Copyright (c) 2025 Andrew Klofas).

### Manufacturing outputs

- **`export(kind=…)`** — one output via kicad-cli: gerbers, drill, pos, bom, netlist, render, step, svg; per-kind settings in `options` (unknown keys are refused; `help("export")` lists them)
- **`export_fab_package`** — one-shot: gerbers + drill (PTH/NPTH split) + pos CSV + BOM + 3D render, all under `<project>/fab/`
- **3D STEP export** — `export(kind="step")` for Fusion 360 / SolidWorks / FreeCAD
- **3D render** — `export(kind="render")` (PNG, configurable side / quality / rotation)
- **SVG per layer** — for documentation
- **BOM with sourcing fields** — `export(kind="bom")` carries the symbols' MPN / manufacturer / distributor fields
- **Panelization** — `panelize_board_grid` duplicates the board in a grid with mouse bites for batch fabrication

### Engineering analysis

- **Signal integrity** — `calc_impedance`: microstrip / stripline (IPC-2141A), differential, and CPWG (conductor-backed CPW, Ghione & Naldi, checked against exact elliptic integrals) impedance from a width, or the width for a target Z₀, for every geometry
- **Thermal capacity** — `calc_trace_current`: IPC-2152 amps for a width or width for a current; `analyze_pcb_current_capacity` per-net audit identifying the weakest segment
- **Thermal network** — `simulate_thermal_steady_state`: lumped-element junction temperature solver (Rjc + Rca → Tj)
- **Board thermal review** — `analyze_board_thermal`: per-part junction temperature on the active board. Power comes from the `review_part_ratings` model (same `loads`, `rail_voltages`, `efficiency` …), Rθ_JA from the extracted datasheet (a JEDEC test board, used as is) or the package-typical estimate corrected by the thermal vias under the exposed pad (vias on the pad's net in 1.5× its box, drill-weighted against the count recommended for the pad area: good ×0.50, adequate ×0.65, insufficient ×0.80, none ×1.0). Then class-II MLCCs and electrolytics within 10 mm of a part rising ≥ 20 K. Rules:

  | Rule | Severity | Meaning |
  |---|---|---|
  | `TH-TJ` | error / warning | Tj above Tj max (warning only on the package estimate) / within `tj_margin_c` (from `derating_profile`) |
  | `TH-VIAS` | warning, info | Exposed pad with fewer than the recommended thermal vias: a part above 0.5 W / any other fitted U, IC, Q with a thermal pad (≥ 2 mm², on ground or a supply; a switch-node tab gets no via advice). A pad drawn on both sides counts once |
  | `TH-MLCC-HOT`, `TH-ELCO-HOT` | info, warning / info | Class-II MLCC (capacitance drift) / electrolytic (life halves per 10 K) within 10 mm of a part whose board under it runs ≥ 20 K above ambient: Tj rise − ψjb·P from the spec cache (`thermal.psi_jb`), an upper bound for the cap. Without ψjb only the junction rise bounds it, and `TH-ELCO-HOT` is then info |
  | `TH-NODATA` | info | A part above 50 mW with no Rθ_JA (unknown package) or no Tj max |
- **Crosstalk** — `estimate_crosstalk`: closed-form NEXT/FEXT for parallel microstrips
- **Return path continuity** — `check_return_path_continuity` checks each signal segment against the filled ground on the adjacent layer(s), skipping pad / via ends and power nets
- **EMC sanity checks** — `analyze_ground_coverage` (filled zone area on board outlines with rounded corners), `find_long_traces` (antenna detection), `validate_decoupling_caps` (cap placement distance)

### EMC pre-compliance review (`analyze_emc`)

Runs on the board's real filled copper: every zone's filled polygons are rasterised onto one grid (`resolution_mm`, default 0.5; even-odd fill, so fractured polygons with holes are exact) and all checks query it. Refill zones before running. Regulators, rail voltages and part data come from the schematic netlist, `analyze_power_tree` and the datasheet spec cache. It is a first-pass estimator for pre-compliance, not a sign-off.

- **`ground`** — each layer's role from its fill (a layer is a ground plane at ≥ 50 % ground fill, ≥ 80 % if it also carries signals — an inner GND layer is never mistaken for a signal layer), adjacent reference plane for every routed layer with the dielectric thickness, floating ground islands, every ground net and the parts that join them, signals crossing a change of reference-plane net without a bridging capacitor, ground-via stitching against λ/20 at `f_max_hz`, ground copper along the board edge, connector ground pins.
- **`switching`** — per switching regulator: switch-node copper area, the input hot loop through the real pad positions, the trapezoidal harmonic envelope, and a differential-mode radiation estimate (Ott, E = K·f²·A·I/r) against `standard` (`fcc-class-b`, `fcc-class-a`, `cispr-class-b`, `cispr-class-a`); switching inductors near the edge or a connector. Duty cycle from the rail voltages (buck only when the switch-node inductor lands on the output). The switching frequency is taken from `fsw={"U1": 500e3, "U2/ch1": 1.8e6}`, else from the datasheet only when the schematic meets the condition the value was stated for (RT pin floating when the condition says RT itself floats, or one resistor to ground of the stated value within 1 %, and any SYNC / CLKIN input idle - grounded, open, on a rail or reached only by test points and resistors to ground; with the datasheet min / max given, `fsw_range_hz` carries the tolerance the harmonics move with; `fsw_source` names it); otherwise the datasheet value is shown as a hint. Load current from `iout=`, else the datasheet IOUT max (worst case, said so).
- **`decoupling`** — every IC supply pin (power_in, from the netlist) to its nearest capacitor on the same net, each cap's ground pad to the nearest ground via, and per-rail capacitor banks against `pdn={"+3V3": 0.05}` or `{"ripple": 0.03, "i_step": 0.5}`. ESL / ESR only from extracted capacitor datasheets — never a generic per-package value.
- **`io`** — every connector line: TVS / ESD diode present and its distance to the pin, series filtering on signal lines; signal tracks near the edge; clocks near connectors; crystal-to-IC distance; diff-pair skew, layer changes and reference-plane continuity; clock / high-speed tracks closer than 3H to another net; connector aperture slot resonance (c / 2L, L from the connector's pad extent) against the known switching harmonics, and the shielding the radiation estimates leave to an enclosure.
- **`thermal`** — class-II MLCCs biased at ≥ 50 % of their `Voltage` rating (the capacitance loss only from an extracted datasheet, never a generic derating table) and ferrites within 10 mm of a switching regulator.
- **Test plan** — always returned: the tests to run for the standard, each switcher's harmonics in the conducted (150 kHz – 30 MHz) and radiated bands, near-field probe points (switch nodes, connectors), and the top risks.
- **Harmonic-limit screen** — `harmonic_limits={"name": …, "unit": "dBuV/m", "distance_m": 3, "bands": [{"f_start_hz", "f_stop_hz", "limit_db", "limit_stop_db"}]}` (or `"unit": "dBuA"` for the harmonic current): your own limit profile, flat or sloped in log f per band — no limit tables are shipped. Each switcher's trapezoid envelope gives the worst margin per band in `harmonic_screen` (`screening_only: true`); an invalid profile is an error.

| Rule | Severity | Meaning |
|---|---|---|
| `EM-GND-NONE` | error | No filled ground copper on any layer |
| `EM-ZONES-UNFILLED` | warning | Zones without fill in the file (refill and re-run) |
| `EM-STACK-REF` | warning | A routed layer has no adjacent plane |
| `EM-GND-ISLAND` | warning | Ground copper island with no via or pad |
| `EM-GND-DOMAINS` | info | Ground nets and the parts joining them |
| `EM-SPLIT-CROSS` | warning | Signal crosses a change of reference-plane net, no bridging cap within 5 mm |
| `EM-REF-GAP` | warning / info | Track with no ground or supply copper on either adjacent layer under it (local check; signal-net pours and the track's own net do not count). Gap stretches are connected runs across segments; a net is reported when one stretch reaches 1 mm. Antipads skipped within each via / through-hole pad's radius + zone clearance + one raster cell; arcs sampled along the chord. Warning for clock / high-speed signals, or a stretch ≥ 5 mm with the plane ≤ 0.5 mm away; info otherwise and for supply nets. Board-wide rule; `check_return_path_continuity` stays a per-segment drill-down |
| `EM-VIA-STITCH` | warning / info | Signal or supply layer change (a via, a through-hole pin, or a via in an SMD pad) that moves the return current to another plane. Each layer's reference is the nearest adjacent pour at the spot, not of the hole's own net; both neighbours when their dielectrics are within 2× (`nearer_plane_share` gives the nearer plane's share of the return). Same plane net: no via of that net reaching both planes within 2× their spacing, at least 1 mm. Ground and supply plane: no capacitor between them within 5 mm. Two ground nets: reported as a split / isolation barrier the signal should not cross. One per net; warning for clock / high-speed signals, info otherwise |
| `EM-STITCH` | warning | < 90 % of the overlapping ground area within λ/40 of a ground via |
| `EM-EDGE-GND` | warning / info | Ground copper covers < 70 % / < 90 % of the board edge |
| `EM-CONN-GND` | warning / info | Connector with fewer than max(2, signals/4) ground pins (warning for high-speed) |
| `EM-SW-AREA` | warning / info | Switch-node copper > 100 / ≥ 25 mm² |
| `EM-SW-CIN` | warning | Nearest input cap > 5 mm from the VIN pin |
| `EM-SW-RAD` | warning / info | Radiation estimate margin < 6 dB / margin, or why it was skipped |
| `EM-HARM-LIMIT` | warning | A switcher harmonic above the caller's `harmonic_limits` profile (screening estimate) |
| `EM-SW-FSW`, `EM-SW-NONET` | info | No switching frequency given / no netlist |
| `EM-SW-L-EDGE`, `EM-SW-L-CONN` | warning | Switching inductor < 3 mm from the edge / < 5 mm from a connector |
| `EM-DEC-DIST`, `EM-DEC-NONE` | warning | Supply pins' nearest cap beyond `decap_max_mm` / no cap on that net (one finding per IC and net) |
| `EM-DEC-VIA` | info | Cap ground pad farther than `cap_via_max_mm` from a ground via |
| `EM-PDN` | info | Rail bank vs target impedance (capacitive asymptote; ESL coverage stated). Every bank, target or not, carries `z_profile_ohm` at 100 kHz – 100 MHz from C, datasheet ESR / ESL and both pads' layout mounting inductance: the full sum only when every cap has ESR and L, values only below 0.3× the lowest resonance when an ESR is missing, none when an L is missing; `z_capacitive_ohm` always. No plane capacitance or spreading inductance; caps behind a ferrite or 0 Ω link are another net |
| `EM-IO-ESD`, `EM-IO-ESD-DIST` | warning (info on power lines) | Connector lines without TVS / ESD diode (one finding per connector, lines in `nets`) / protection farther than `esd_max_mm` |
| `EM-IO-FILTER` | info | Signal lines leave the board with no series R / ferrite / inductor (one finding per connector) |
| `EM-EDGE-TRACE` | warning / info | Signal track within `edge_keepout_mm` of the edge (warning for clock / high-speed) |
| `EM-CLK-CONN` | warning | Clock passes < 10 mm from a connector |
| `EM-XTAL-DIST` | warning | Crystal > 10 mm from the IC it drives |
| `EM-DIFF-SKEW`, `EM-DIFF-REF` | warning | Pair skew > `diff_skew_ps` / reference plane changes net or has gaps under the pair |
| `EM-DIFF-LAYERS` | info | Pair routed on more than one layer |
| `EM-3H` | warning | Clock / high-speed track parallel to another net closer than 3H |
| `EM-SHIELD-SLOT`, `EM-SHIELD-SE` | warning, info | Connector aperture resonance near a known harmonic / shielding needed for 6 dB margin |
| `EM-TH-BIAS`, `EM-TH-FERRITE` | info | Class-II MLCC at ≥ 50 % of rating / ferrite < 10 mm from a switcher |

### Simulation

- **SPICE** — `export_spice_netlist` + `run_spice` (transient / DC / AC / noise) on ngspice or LTspice (`simulator`: `auto` = ngspice, else LTspice). LTspice is found through `LTSPICE_PATH`, absolute PATH entries (never a relative one, so nothing in the project folder runs), then the default install folders, launched by its full path in batch mode (`-b`); its binary `.raw` output is read directly (UTF-16 header, complex data for AC). The same backends drive `simulate_subcircuits` (`simulator=`), which names the one it used.
- *Note: requires symbols with `Spice_*` fields and ngspice (`brew install ngspice`) or LTspice installed*
- **Automatic subcircuit checks** — `simulate_subcircuits` finds the passive networks in the schematic itself (no `Spice_*` fields needed) and checks them against the design intent:
  - `divider` — resistor dividers into an active pin: ratio, tapped voltage, worst case from each resistor's `Tolerance` field; for a regulator FB divider, Vout typ and worst case from the datasheet Vref min / max in the spec cache, plus Monte Carlo (`monte_carlo=N`);
  - `rc` — RC low-pass nodes feeding an active input (not supply rails): corner frequency with the node's Thevenin resistance; first-order high-pass nodes (series cap into resistance to ground or a rail; feedback nodes and nodes also held by an IC output or a transistor skipped) in `rc_highpass`: a single series source resistor adds to R, the caller's `loads` on the node goes in parallel (a 50 Ω instrument sets the corner) or the result says open load, and `role` names it `dc_block` (into a connector), `timing` (a rail into an IC input: power-on reset, soft-start) or `ac_coupling`; first-order formula, compared by `what_if`; op-amps also with bare `+` / `-` pins (on parts whose library, value or description reads as an amplifier or comparator) and an unnamed output or open-collector pin;
  - `lc` — LC / ferrite / π ladders (series L between nodes with capacitors to ground; converter switch nodes excluded), with R–C damping branches, run as a SPICE AC sweep (ngspice or LTspice) from the ladder's source end to its far end: resonance peaks and attenuation at `freqs` and at the 1st–3rd harmonic of each `fsw`; Monte Carlo over L / C tolerances; optional `loads={net: Ω}` and `source_ohm`.

  - `opamp` — op-amp stages from pin names (`+IN` / `IN+` / `1IN-` / `-INA` / `OUTA` / bare `+` `-` …; a single-channel symbol's unnamed output pin is OUT): follower, non-inverting or inverting, gain and noise gain from the feedback network, feedback-capacitor pole, and the closed-loop bandwidth GBW / noise gain when the spec cache has the op-amp's `GBW`. With a vendor model — `opamp_models={"U1": {"lib": "OPA1612.lib", "subckt": "OPA1612", "pins": ["+IN", "-IN", "V+", "V-", "OUT"]}}` (roles in `.subckt` node order, else read from the node names; a subcircuit without `.ends` is refused with that cause, not left to an ngspice error) or the symbol's KiCAD `Sim.Library` / `Sim.Name` / `Sim.Pins` fields — each stage is simulated with a SPICE AC sweep on its real feedback network and supplies (rail voltages or `supplies=`): gain, −3 dB frequency, peaking; without one, a behavioural model is built from the op-amp's extracted specs (`GBW` and `Aol` required, `Vos`, `R_in` optional; single pole at GBW / Aol, labelled `behavioural, from datasheet <MPN> p.N`); without those keys the amplifier is ideal and the result names the missing keys. Behavioural LDO (Vref, dropout, Iq) and reference (Vref, Z_out) models come from the same spec keys (`adapters/spice_models.py`);
  - `crystal` — a crystal with a load cap on each pin: effective load C1·C2/(C1+C2) + `c_stray_pf` (no stray value is assumed) against the crystal's `CL` from the spec cache (warning beyond ±10 %).

  Inductor DCR, capacitor ESR / ESL and L / C tolerances come from the datasheet spec cache when a part has them (a `Tolerance` field wins), and a capacitor with a `C_bias` curve is derated at its rail's DC voltage (`dc_bias_ratio` in the result); `pcb_parasitics=True` adds each ladder capacitor's mounting inductance on both pads (a supply pad with no via of its rail within 3 mm is left out and named in `sides_missing`): each via barrel from the cap's layer to the first plane of its net (L = 0.2·h·(ln(4h/d) + 1) nH) plus the pad-to-via connection as a strip over the plane (track width, or the pad width for a pour; inductance per length from Hammerstad's microstrip Z0). Otherwise parts are ideal — with an open load the peaking is an upper bound and is reported as info. Rules: `SP-DIV-VOUT`, `SP-TOL` (no Tolerance field), `SP-LC-PEAK` (peaking above `peaking_warn_db`, warning only with loads given), `SP-LC-ATT`, `SP-OPAMP`, `SP-XTAL-CL`, `SP-NOSIM` (no simulator).

### Release: Gerbers, diff, what-if, lifecycle, BOM, gate

- **`analyze_gerbers`** — reads the exported Gerber (RS-274X / X2) and Excellon folder as the fab will: per file the function, polarity, apertures, draws / flashes / regions and extent; per drill file the tool table with hits. Cross-checks the `.gbrjob`: files listed but missing (`GB-MISSING-FILE`, error only for fabrication layers), copper layer count (`GB-LAYERS`), outline extent vs board size (`GB-SIZE`); missing outline / drill (`GB-NO-OUTLINE`, `GB-NO-DRILL`), holes outside the outline (`GB-DRILL-OUTSIDE`), Gerbers older than the board file (`GB-STALE`), and with `preset` the narrowest conductor draw and smallest drill against the fab minimum (`GB-MIN-TRACE`, `GB-MIN-DRILL`).
- **`diff_designs`** — two revisions of the active project (`git:<ref>`, a folder, or the working tree; git refs go to a temporary folder, read-only). Schematic, from KiCAD's netlists: parts added / removed, Value / Footprint / lib / DNP / field changes, net renames, pins moved to another net, nets added / removed. PCB: footprints added / removed / moved / rotated / flipped / re-footprinted, routing changes per net (length, segments), vias, zones per net and layer, board size.
- **`what_if`** — `changes={"R7": "120k", "C42": {"dnp": true}}` re-runs `analyze_power_tree`, `review_connectivity`, the divider / RC checks, (with a simulator) the LC / π ladders and (with `emc=True`) the netlist-driven `analyze_emc` groups on the unchanged copper, on a changed copy of the netlist (no file touched): regulator Vout and enable changes, rail voltages, divider outputs, RC corners, filter peaking and attenuation at the `fsw` harmonics, switcher radiation margins, and the findings that appear or disappear.
- **`audit_lifecycle`** — every schematic MPN at DigiKey / Mouser / element14-Farnell (exact match; `jlcpcb` adds JLC stock): lifecycle (DigiKey `ProductStatus` + Discontinued / EndOfLife, Mouser `LifecycleStatus` + `SuggestedReplacement`, Farnell `productStatus`) and stock. `LC-EOL` (error; includes Farnell `NO_LONGER_MANUFACTURED`), `LC-NRND`, `LC-RESTRICTED`, `LC-STOCK` (warnings), `LC-SINGLE`, `LC-NOTFOUND`, `LC-DIST-DROP` (info: a distributor stopped stocking it, `NO_LONGER_STOCKED`). End-of-life and NRND parts get `alternatives`: each source's suggested replacement, then the MPN's family stem searched at the queried sources (same manufacturer, same package token, not EOL / NRND), each with source, stock and lifecycle — candidates to check, never replaced automatically. Temperature grade per part — operating range from the extracted datasheet (`recommended.T_A`, else `T_J`), else the distributor's operating-temperature field, classified commercial / industrial / extended / automotive / military; with `temp_range_c=[-40, 85]` a part that does not cover it is `LC-TEMP` (warning) and parts without a range are listed in one `LC-TEMP-NODATA` (info).
- **`review_assembly`** — real (also non-convex) courtyard outlines that overlap (`AS-CRTYD-OVERLAP`; touching is fine) or sit closer than `min_spacing_mm` (`AS-CRTYD-SPACING`), courtyards beyond or near the board edge (`AS-EDGE`, info for connectors), no courtyard (`AS-NO-CRTYD`), SMD parts on a side the assembler will not populate (`assembly_sides` top / bottom / both, `AS-SIDE` error; the default `auto` takes the board's own sides and notes a second reflow side as `AS-SIDE` info), through-hole parts (`AS-THT`), fiducials per populated side (`AS-FIDUCIAL`), test-point coverage with the supply rails that have no test point (`AS-TESTPOINT`, info), 0201 / 0402 passives with one pad joined solid to a pour (pad, footprint or zone setting, the pad centre in the fill) or on a via in pad that lands in its net's pour on another layer, and the other on a track (`AS-TOMBSTONE`, warning) or a thermal relief (info), or track widths differing > 3× (info); thermal reliefs on both pads are the remedy and are not reported, and a `zone_connection` rule in the board's `.kicad_dru` is stated, not evaluated; per-side passive orientation as data (`orientation`, no finding: pick-and-place takes any angle), polarized parts with no silkscreen of their own (`AS-SILK-POLARITY`) or no asymmetric mark at pad 1 — a silk point near pad 1 whose point reflection through the pads' centre has no counterpart: pin-1 dot, cathode bar, chamfer (`AS-PIN1-MARK`); parts standing in the straight run (5 mm or more, a minimum spanning tree over the net's parts) of a supply net — VIN, +24V, LED_24V, VBUS, … — with no pad on it, a floorplan that splits the power path (`AS-POWER-PATH`, info); part heights from each footprint's 3D STEP model (offset / scale / rotation applied; `KICAD{N}_3DMODEL_DIR` resolved): tall parts near the edge (`AS-TALL-EDGE`), parts above `max_height_top_mm` / `max_height_bottom_mm` (`AS-HEIGHT`, enclosure clearance), model paths that do not resolve (`AS-3D-MISSING`), no model (`AS-3D-NONE`) — heights are the model's, not the fitted part's datasheet; and schematic ↔ PCB cross-verify: parts on one side only (`XV-MISSING` / `XV-EXTRA`), symbol pins without a pad of that number (`XV-PADS`, error), Value / Footprint / MPN differing between schematic and board (`XV-FIELD`). With `dfm_preset` (a `list_fab_presets` name) the copper is checked against what that fab can build — DRC checks the project's own rules, this checks the fab's — with a 0–100 `dfm_score` (−10 per error, −3 per warning, capped per rule) and its deductions: `DFM-TRACK`, `DFM-SPACING` (two nets' tracks / pads / vias on one layer), `DFM-DRILL`, `DFM-VIA`, `DFM-RING` (via ring below (min via diameter − min drill) / 2), `DFM-EDGE` (copper to Edge.Cuts) — errors; `DFM-VIA-IN-PAD` (not exposed pads), `DFM-SILK` (silkscreen over a pad), `DFM-MASK` (mask web between pads below the preset's `min_solder_mask_bridge` or the board's `solder_mask_min_width`; listed as not checked otherwise) — warnings. Zones are left to DRC. Always returned: `complexity` — placements, unique parts, sides, THT, fine pitch (< 0.5 mm) and BGA / exposed-pad parts, smallest passive, DNP count, and a 0–100 score with its factors (package mix, unique parts above 15, both sides, THT with SMD, fine pitch / BGA).
- **`diff_bom`** — the BOM of two revisions (`git:<ref>`, a folder, or the working tree): designators added / removed, Value / Footprint / MPN / Manufacturer / LCSC / distributor numbers / DNP / exclude-from-BOM changes (`extra_fields` for more), quantity changes per BOM line (MPN, or value + footprint), MPNs new or gone.
- **`export_fab_bom`** — assembly BOM + CPL in the fab's columns: `jlcpcb` (`Comment, Designator, Footprint, LCSC Part #`; `FB-NO-LCSC`) or `pcbway` (`Item #, Designator, Qty, Manufacturer, Mfg Part #, Description / Value, Package/Footprint, Type, Your Instructions / Notes`; `FB-NO-MPN`, `FB-NO-MFR`), CPL `Designator, Mid X, Mid Y, Layer, Rotation` from `kicad-cli pcb export pos`. DNP, excluded-from-BOM, test points, holes, fiducials and `exclude_refs` (consigned parts) are left out. Rotation offsets: `rotation_offsets={"^SOT-23": 180}` (footprint regex) or a `JLCPCB Rotation Offset` / `Rotation Offset` symbol field — no fab table is assumed.
- **`update_symbol_fields`** — set fields on many symbols across the whole sheet hierarchy from `updates={"R7": {"MPN": "...", "Manufacturer": "..."}}` and / or a CSV (Reference / Designator column, one column per field). Dry run first (old → new list); `dry_run=False` writes each changed sheet through the guarded writer, every unit of a multi-unit symbol. Reused sheets are reported (a field is shared by every instance); `Reference` is refused.
- **`fab_release_gate`** — one verdict, `FAB-READY` or `NOT FAB-READY`, with blockers and warnings: ERC errors; DRC errors, unconnected items and schematic parity (run without refilling or saving); errors from the power tree and connectivity reviews; the Gerber folder (errors, stale Gerbers, preset minimums); MPN coverage (blocking only with `require_mpn`); assembly and cross-verify errors (with the default `assembly_sides="auto"` a board with SMD parts on both sides adds a warning to confirm the assembler populates both); with `preset` the DFM check (`DFM-*` errors block, `dfm_score` reported); with `loads` the board thermal review (`TH-TJ` errors block); optionally the lifecycle audit (`LC-EOL` blocks). Read-only.

### Multi-board projects

- `add_board`, `set_active_board`, `list_boards` — manage several `.kicad_pcb` files in one project (main + breakout + debugger), every PCB tool honors the active board.

---

## Example workflow: designing a board

A USB-C powered sensor board (USB-C input, 3.3 V buck, MCU, I2C sensor),
from an empty folder to a board that passes the release gate. Each phase
shows the prompt you give Claude Code and the tool calls it makes. You
answer the decisions (part choice, fixes), Claude runs the tools.

**0. Project and tool sets**

```
> Create a project at D:/work/sensor and load what we need for schematic work.

create_project   path="D:/work/sensor"  name="sensor"
load_toolset     toolsets=["library", "sch", "datasheets", "sourcing"]
```

**1. Part selection**: pick parts from KiCAD's libraries and check that they can be bought.

```
> Find a symbol and footprint for a TPS62162 buck, an STM32G031 and an SHT40.
> Check stock for each.

index_libraries                                   # one-time, ~60 s
search_symbol       query="TPS62162"
get_symbol_details  lib_id="Regulator_Switching:TPS62162DSG"
search_footprint    query="WSON-8 2x2"
check_availability  mpn="TPS62162DSGR"  sources=["digikey", "mouser", "jlcpcb"]
```

**2. Schematic capture**: place, wire, annotate, ERC.

```
> Draw the schematic: USB-C 5V into the buck, 3V3 to the MCU and the sensor,
> I2C between them with pull-ups. Put the MPN on every part.

add_symbol        lib_id="Regulator_Switching:TPS62162DSG" reference="U?" value="TPS62162"
                  x_mm=101.6 y_mm=76.2 footprint="Package_SON:WSON-8-1EP_2x2mm_P0.5mm_EP0.9x1.6mm"
                  fields={"MPN": "TPS62162DSGR", "Manufacturer": "Texas Instruments"}
add_power_symbol  net="+5V"   x_mm=88.9  y_mm=63.5
add_power_symbol  net="GND"   x_mm=101.6 y_mm=101.6
list_pins         reference="U?"                  # pin numbers and names
get_pin_position  reference="U?" pin="1"          # exact wire endpoints
add_wire          x1_mm=88.9 y1_mm=63.5 x2_mm=88.9 y2_mm=73.66
add_label         net="I2C_SDA" x_mm=152.4 y_mm=88.9
add_no_connect    reference="U?" pin="8"            # PG unused
annotate_schematic                                # U? → U1, R? → R1 …
find_dangling     scope="all"
run_erc                                           # repeat until 0 errors
```

**3. Datasheets**: the review checks need the parts' real limits, not general knowledge.

```
> Download the datasheets and extract the specs of the ICs.

sync_datasheets               download=True       # datasheets/ + manifest
get_datasheet_extraction_task mpn="TPS62162DSGR"  # pages, pins, schema, PDF SHA-256
save_datasheet_extraction     mpn="TPS62162DSGR"  extraction={…}  pdf_sha256="…"
get_datasheet_specs                               # coverage and quality per MPN
```

Claude reads the pages the task returns and fills the record; the server
checks every number against the cited page before it is cached.

**4. Schematic review**: run the checks, fix, re-run.

```
> Review the schematic. VBUS is 5 V, the board draws 150 mA from 3V3 at 50 °C.

load_toolset         toolsets=["review", "sim"]
analyze_power_tree   rail_voltages={"/VBUS": 5}
review_connectivity  derating_profile="industrial"
review_circuits      groups=["power", "digital"]  i2c_mode="fast"
review_part_ratings  loads={"+3V3": 0.15}  ambient_c=50
simulate_subcircuits types=["divider", "rc", "lc"]
what_if              changes={"R3": "180k"}      # try a fix before you write it
```

Each finding has a rule ID (tables above). Fix it in the schematic,
run the tool again, and stop when no errors are left.

**5. Layout**: stackup, fab rules, net classes, placement, routing.

```
> Make a 40×25 mm 4-layer board for JLCPCB. USB D+/D- as a 90 Ω pair.
> Connector on the left edge, buck close to it, GND plane on In1.Cu.

load_toolset         toolsets=["pcb", "rules", "emc_si"]
set_layer_count      n=4
set_board_outline    width_mm=40 height_mm=25
apply_fab_preset     preset="jlcpcb_4l"  dry_run=True     # then without dry_run
calc_impedance       kind="differential" dielectric_height_mm=0.2 target_ohms=90 gap_mm=0.15
add_diff_pair_class  name="USB" diff_pair_width_mm=0.2 diff_pair_gap_mm=0.15
assign_net_class     net_pattern="/USB_D*"  class_name="USB"
update_pcb_from_schematic                                 # footprints + nets
move_footprint       reference="J1" x_mm=2 y_mm=12.5 rotation=90
add_ground_plane     layer="In1.Cu"  net="GND"
autoroute_pcb        passes=20
list_unrouted        summary=True
run_drc              refill_zones=True  schematic_parity=True
```

**6. Layout review and DFM**: EMC, thermal, assembly, lifecycle.

```
> Review the layout for EMC, heat and assembly at JLCPCB, -40…85 °C parts.

analyze_emc            standard="cispr-class-b"  fsw={"U1": 2.25e6}
analyze_board_thermal  loads={"+3V3": 0.15}  ambient_c=50
review_assembly        dfm_preset="jlcpcb_4l"
audit_lifecycle        sources=["digikey", "mouser"]  temp_range_c=[-40, 85]
```

**7. Release gate and change review**

```
> Is the board ready for fab? Show what changed since main.

fab_release_gate  preset="jlcpcb_4l"  loads={"+3V3": 0.15}  check_lifecycle=True
diff_designs      old="git:main"
diff_bom          old="git:main"
```

`fab_release_gate` returns `FAB-READY` or `NOT FAB-READY` with the
blockers. Open the result in KiCAD and commit it. The fab outputs
(`export_fab_package`, `export_fab_bom`) are in the `fab` and `sourcing` sets.

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
apply_fab_preset  preset="jlcpcb_4l"
add_diff_pair_class name="USB" diff_pair_width_mm=0.20 diff_pair_gap_mm=0.18
add_net_class       name="Power" track_width_mm=0.5 clearance_mm=0.25
assign_net_class    net_pattern="USB_*"  class_name="USB"
assign_net_class    net_pattern="+5V"    class_name="Power"

update_pcb_from_schematic                                  # footprints + nets
move_footprint    reference="R1" x_mm=20 y_mm=15
move_footprint    reference="R2" x_mm=20 y_mm=18
add_ground_plane  layer="B.Cu"  net="GND"
autoroute_pcb     passes=20

# 3. Validate + export
run_erc                                                    # 0 errors
run_drc           refill_zones=True  schematic_parity=True  # 0 errors
export_fab_package  include_render=True
enrich_bom_with_sourcing  sources=["mouser", "digikey", "tme", "farnell"]
```

---

## Architecture

```
kicad-claude-mcp/
├── server.py                          # FastMCP entry point — core set + toolset control
├── pyproject.toml                     # uv project
├── .env.example                       # API keys + path overrides
├── src/kicad_claude/
│   ├── strict_args.py                 # unknown-argument errors + accepted aliases
│   ├── state.py                       # active project / sheet / board
│   ├── toolsets.py                    # tool sets: registry, load/unload, list_changed
│   ├── arg_types.py                   # Literal choice types shared by tool signatures
│   ├── tools/                         # MCP tools, one module or package per tool set
│   │   ├── common.py                  # active-project helpers shared by several sets
│   │   ├── core/                      # project.py (create/set/state/list_components/locks), validation.py (run_erc, run_drc)
│   │   ├── sch/                       # schematic.py (symbols, wires, labels, buses, sheets, fields, nets), sync.py (annotate, update_pcb_from_schematic)
│   │   ├── pcb/                       # layout.py (board edit/query, removal, diff pairs, multi-board), routing.py (Freerouting), rf.py
│   │   ├── rules.py                   # design rules, net classes, fab presets, DRU
│   │   ├── library/                   # index.py (index_libraries, search_*), create.py (create_symbol, create_footprint)
│   │   ├── review/                    # design.py (power tree, connectivity, circuits, ratings), release.py (gerbers, diff, what_if, lifecycle, assembly, gate)
│   │   ├── emc_si/                    # emc.py (analyze_emc + heuristics), signal_integrity.py (calc_impedance, crosstalk, return path), thermal.py (incl. analyze_board_thermal)
│   │   ├── sim.py                     # SPICE netlist, run_spice, simulate_subcircuits
│   │   ├── datasheets.py              # sync_datasheets, extraction task / save / specs
│   │   ├── sourcing/                  # distributors.py (availability, datasheets, vendor ZIPs, enrichment), bom.py (diff, fab BOM, field write-back)
│   │   └── fab/                       # exports.py (export, export_fab_package), panelization.py
│   ├── adapters/                      # logic detached from MCP framing
│   │   ├── sch_io.py                  # parse + write
│   │   ├── kicad_prettify.py          # port of KiCAD's Prettify (byte-exact layout)
│   │   ├── safe_write.py              # lock check, backup, re-parse, restore
│   │   ├── sch_editor.py              # schematic tree mutations
│   │   ├── sch_netlist.py             # schematic connectivity model
│   │   ├── pcb_editor.py              # PCB tree mutations
│   │   ├── pcb_geometry.py            # outlines, zones, polygons
│   │   ├── pcb_layers.py              # layer table and stackup blocks
│   │   ├── pcb_netlist.py             # copper connectivity (ratsnest)
│   │   ├── kicad_cli.py               # subprocess wrapper for kicad-cli
│   │   ├── kicad_python.py            # bridge to KiCAD's bundled Python (pcbnew)
│   │   ├── freerouting.py             # JAR runner with timeout + log parser
│   │   ├── power_review.py            # regulators, rails, Vout, datasheet limit checks
│   │   ├── connectivity_review.py     # power pins, pull-ups, I2C, pinout, abs max, caps
│   │   ├── circuit_review.py          # power / analog / digital / misc circuit detectors
│   │   ├── domain_review.py           # interfaces / power conversion / drives / RF + certification
│   │   ├── part_ratings.py            # per-rail current model, ratings, Tj, LED strings
│   │   ├── thermal_pkg.py             # package-typical Rθ_JA (labelled estimate)
│   │   ├── board_thermal.py           # Tj with thermal vias, hot-spot proximity
│   │   ├── dfm.py                     # DFM check against a fab preset + score
│   │   ├── derating.py                # derating profiles (policy values)
│   │   ├── temp_grade.py              # operating-temperature ranges and grades
│   │   ├── dc_model.py                # DC operating point: resistor networks, clamps, zeners, switch scenarios
│   │   ├── bom_tools.py               # BOM diff, JLCPCB / PCBWay BOM + CPL, field write-back
│   │   ├── emc_review.py              # EMC review on rasterised copper + test plan
│   │   ├── spice_auto.py              # divider / RC / LC-pi detection, SPICE AC, Monte Carlo
│   │   ├── spice_backends.py          # ngspice / LTspice backends, .raw reader
│   │   ├── spice_models.py            # behavioural op-amp / LDO / reference models from specs
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
│   │   ├── sch_sync.py                # place missing footprints, sync fields, orphans
│   │   ├── drc_rules.py               # .kicad_dru read/write
│   │   ├── project_settings.py        # .kicad_pro JSON helpers
│   │   ├── vendor_import.py           # ZIP extraction + lib-table updates
│   │   ├── snapeda.py                 # URL helpers + manual fallback
│   │   └── length_tuning.py           # meander geometry generator
│   ├── indexer/                       # KiCAD library indexing (~10 MB cache)
│   ├── templates/                     # blank project / sheet / PCB
│   └── utils/                         # logging, geometry (mm, grid snap), paths
├── tests/                             # 995 fast + 72 acceptance + 6 network tests
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
- **Freerouting 2.5.0 needs Java 25+.** It is the tested version; the pass count goes in `--router.autorouter.max_passes` (2.5 deprecates `-mp`) and the stats are read from its log format.
- **Closed-form analyses are estimators.** Impedance / current / crosstalk / thermal / return-path tools give ballpark numbers good for early design. Production hardware needs Ansys / Sonnet / Saturn PCB / Icepak for sign-off.
- **Design review is only as good as its inputs.** Datasheet checks run only for parts with an extracted spec record; a net's voltage is known only from regulator outputs, series links, the DC operating-point model (resistor networks, clamps, zeners, switch scenarios) and the `rail_voltages` you pass. The model is static: the accepted limits are listed in OP-13 of `docs/open_points.md`.

---

## Development

```bash
uv run mcp dev server.py                          # MCP Inspector (browser UI)
uv run pytest -m "not slow and not network" -q    # 995 fast tests, ~2 min
uv run pytest -m "slow" -q                        # 72 acceptance tests via kicad-cli (~8 min)
uv run pytest -m "network" -q                     # 6 live distributor tests
```

The slow tests need KiCAD installed and the library index built (`index_libraries`
once). Network tests need `.env` with valid DigiKey OAuth credentials and a Mouser
*Search API* key (Mouser issues two keys per account — Search and Order — only
the Search one works here).

---

## Acknowledgements

Built on top of:

- [`sexpdata`](https://pypi.org/project/sexpdata/) for parsing KiCAD's S-expression format
- [`Freerouting`](https://github.com/freerouting/freerouting) for autorouting
- KiCAD's bundled `pcbnew` Python API for Specctra DSN/SES interchange and netlist application
- `kicad-cli` for ERC, DRC, and manufacturing exports
- DigiKey V4 API, Mouser V2 API, element14 / Farnell, TME and JLCPCB for live sourcing data
- [`pypdf`](https://pypi.org/project/pypdf/) for datasheet text
- [`pypdfium2`](https://pypi.org/project/pypdfium2/) to rasterize board and sheet renders
- kicad-happy (MIT, Copyright (c) 2025 Andrew Klofas) — review rule ideas and pin-name vocabulary; ported in part (marked in the code): datasheet value verification, page selection, the package Rθ_JA table, thermal-via correction, derating-profile fractions, temperature grades, alternative-part search, the LTspice process handling, behavioural model shapes, the harmonic-limit profile check

---

## License

MIT — see [`pyproject.toml`](./pyproject.toml).
