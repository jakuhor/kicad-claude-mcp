# Open points — round 2

Gathered on 2026-09-19 from an end-to-end run of the design flow the server
claims to support: create a project, create a symbol and a footprint, place and
wire a schematic, fill the sourcing fields, annotate, run ERC, push the netlist
to the PCB, place and route, review, then export 3D and fabrication outputs.

The run built a real two-layer board (LDO + two caps + LED + resistor + 2-pin
header) in a scratch directory and drove every tool through the same
`register(mcp)` entry points the MCP server uses. KiCad 10.0.6, board format
`20260206`, schematic format `20260306`.

**Status: P1–P15 are all closed.** P1–P13 came from the end-to-end run and
were worked the same day; P14 and P15 were found later on 2026-09-19 by
checking two specific questions. Each point keeps its diagnosis and records
what was done.

**What already worked.** Project creation, symbol and footprint creation,
symbol placement with autoplaced fields, arbitrary symbol properties (`MPN`,
`Manufacturer`, ...), wires, junctions, power symbols, labels, annotation of
`?` references, `list_sch_nets` / `trace_net` / `find_dangling`, board outline,
layer count, footprint placement and moves, `validate_decoupling_caps`, DRC
(including schematic parity), Gerbers, drill, position file, BOM, netlist,
STEP, 3D render, `export_fab_package`, backups through `safe_write`, and
formatting preservation — a symbol added and then removed leaves a
byte-identical file, and every write produced a minimal diff.

---

## P1 — `run_erc` always reported zero violations (Critical) — DONE

`adapters/kicad_cli.py` `_shape_erc` read `data["violations"]`. KiCad 10's ERC
JSON (`https://schemas.kicad.org/erc.v1.json`) has no top-level `violations`
key — violations are nested per sheet:

```
{"source": ..., "sheets": [{"path": "/", "uuid_path": ..., "violations": [...]}]}
```

The key was therefore always missing, and the tool reported
`errors: 0, warnings: 0, total_violations: 0` for every schematic. Measured on
the test board, with an unconnected LM358 and a dangling wire in place,
`run_erc()` said 0 while `kicad-cli sch erc --severity-all` on the same file
found 6. The MCP call even wrote the correct raw report; only the shaping
dropped it. That is the one failure mode nobody notices, because the project's
rule is to prove a write valid with `kicad-cli` and ERC was certifying every
schematic as clean.

**Fixed.** `_erc_violations` walks `data["sheets"][*]["violations"]`, tags each
violation with the `sheet` it came from, and still reads a flat `violations`
list so older reports keep working. The result gains a `sheets` list with a
per-sheet error/warning breakdown.

`run_drc` was checked against the DRC schema rather than assumed: that one does
keep `violations`, `unconnected_items` and `schematic_parity` at the top level,
so it was already right.

Verified on the round-2 test board: `run_erc()` now returns 3 errors and 3
warnings — the same 6 `kicad-cli` reports.

## P2 — the PCB net model was the pre-KiCad-10 one (Critical) — DONE

KiCad 10 (board `20260206`) no longer writes a top-level
`(net <index> "<name>")` table. Pads, zones and tracks carry the net **by
name**: `(pad "1" smd rect … (net "GND"))`, `(zone (net "GND") …)`. A board
saved by `pcbnew` contains zero top-level `(net …)` declarations.

`pcb_editor.list_nets` (and `find_net_index`) only recognised
`(net <int> "<name>")`, so on a KiCad 10 board they saw nothing, and
`pcb_netlist` keyed its whole connectivity graph on the integer, which is 0 for
every pad on such a board. Everything built on them degraded silently:

| Tool | Observed on the test board | Truth |
|---|---|---|
| `list_nets` | `{"nets": []}` | 4 nets, 13 pads assigned |
| `net_route_status(net_name="GND")` | `KeyError: no net named 'GND' on this board` | GND exists |
| `list_unrouted` | `count: 0` ("fully routed") | DRC: 9 unconnected items |
| `get_pad_position` | `net_number: 0` for `/+3V3` | pcbnew reports netcode 3 |
| `analyze_ground_coverage` | `"no GND zone on B.Cu"` | a GND zone is on B.Cu |

Two writers made it worse: `add_zone` wrote the legacy pair `(net 0)` +
`(net_name "GND")` plus a fabricated top-level `(net 0 "GND")` — index 0 is
reserved for the unconnected net — and `add_track(net=…)` took an integer index
that exists nowhere in a KiCad 10 file and wrote it unchecked.

**Fixed.** Net identity is a name everywhere:

- `pcb_editor.read_net_node` reads all three spellings (`(net 3 "GND")`,
  `(net "GND")`, `(net 3)`); `net_of(node, tree)` resolves the name, falling
  back to a zone's `net_name` child and, on a board that still has a table, to
  the table entry for an index-only reference.
- `list_nets` collects nets from the table when there is one and otherwise from
  the pads, zones, tracks and vias themselves; `index` is None for a net with
  no table entry, and the unconnected net is not listed. `net_exists` and
  `has_net_table` answer the two questions callers actually have.
- `net_ref(tree, net)` returns what to write into a new `(net …)` node — an
  index on a legacy board, the name on a KiCad 10 one — and raises `KeyError`
  naming the known nets when the net is not on the board. `add_track`,
  `add_via`, `add_via_array_along_line` and `add_meander_segments` take a name
  (an integer still works) and go through it, so a track can no longer be
  written onto a net that exists nowhere.
- `add_zone` writes `(net "<name>")` on a KiCad 10 board and the legacy pair on
  a board with a table, allocating a table entry when the net has none. It
  never fabricates a `(net 0 "<name>")` declaration.
- `pcb_netlist` keys connectivity on the net name, so `list_unrouted`,
  `net_route_status` and `build_connectivity` work on both formats.
- `emc.analyze_ground_coverage` and `emc.find_long_traces`, `thermal` and
  `simulation` read their nets through `net_of`. Copper with no net is reported
  under one `(no net)` bucket rather than dropped — it still radiates and still
  has a current limit.

Verified: on the test board `list_nets` reports all 4 nets, `list_unrouted`
counts 9 — the same number KiCad's DRC reports — and the slow acceptance gate
(`test_unrouted_count_matches_kicad_drc`) still matches DRC on all five demo
boards.

## P3 — Windows library auto-detection never found KiCad (Major) — DONE

`utils/kicad_paths._KICAD_VERSIONS = ("10", "9", "8", "7")` built
`C:\Program Files\KiCad\10\share\kicad\symbols`. The installer uses `10.0`,
`7.0`, … — with a dot — so `find_symbol_lib_dirs()` and
`find_footprint_lib_dirs()` returned `[]` on a stock Windows install, and only
a `KICAD_LIBRARY_PATH` entry hid it. The `.env` in this repo has the variable
present but empty, so a fresh checkout indexed nothing.

The failure was also silent and destructive: `index_libraries(force=True)` with
no directories found built an empty index and **overwrote**
`~/.cache/kicad-claude/index.json` with `0 symbols, 0 footprints`. That
happened during the review and had to be repaired by hand.

**Fixed.** `_windows_share_dirs(leaf)` lists the real version directories under
every `Program Files\KiCad`, newest first, exactly as `_platform_default_cli_paths`
already did for `kicad-cli`, and keeps the bare names as a fallback. And
`index_libraries` refuses to save an index that found neither a symbol nor a
footprint: it raises, names the directories it searched, and leaves the
previous cache alone.

Verified without any environment variable set: `find_symbol_lib_dirs()` returns
`C:/Program Files/KiCad/10.0/share/kicad/symbols` and a forced re-index finds
223 libraries.

## P4 — libraries the server creates were not usable by the server (Major) — DONE

`create_symbol` writes `<project>/lib/<Lib>.kicad_sym` and registers it in the
project's `sym-lib-table`; `create_footprint` does the same for
`<project>/lib/<Lib>.pretty` and `fp-lib-table`. Neither path was ever indexed:
`indexer.build_index` walks the global directories only. `add_symbol` and
`add_footprint` resolve a `lib_id` exclusively through the index, so the
sequence the tools advertise failed:

```
create_symbol("E2ELib", "MYREG", ...)  -> OK
index_libraries(force=True)            -> 224 libs, 22874 symbols (E2ELib absent)
add_symbol("E2ELib:MYREG", "U1", ...)  -> KeyError: unknown lib_id 'E2ELib:MYREG'
```

**Fixed.** `tools/library._ensure_index` overlays the active project's own
libraries on the global index: `<project>/lib`, plus every directory named by
the project's `sym-lib-table` / `fp-lib-table` with `${KIPRJMOD}` expanded. The
overlay is read fresh on every lookup rather than cached, because a project
library changes under the server's own hands, and it is small. `index_libraries`
reports the merged totals.

Verified with no `KICAD_LIBRARY_PATH` at all: `create_symbol` →
`add_symbol` → `create_footprint` → `add_footprint` with no re-index in
between, and `search_symbol` finds the new part.

## P5 — `set_project` raised on any project with a populated PCB (Major) — DONE

`tools/project.py` imported `PCB` and `Schematic` from `kicad-skip` for the
symbol / footprint / net counts. `kicad-skip` cannot parse a KiCad 10
footprint's text items:

```
File ".../skip/sexp/parser.py", line 618, in __init__
    for i in pv._base_coords:
AttributeError: 'Symbol' object has no attribute '_base_coords'
```

So `set_project` raised on any project whose `.kicad_pcb` held a footprint —
that is, on any real project. `state.set_active` runs first, so the project
*was* active and the following calls worked, which made the error look random.
`get_project_state` and the summary returned by `create_project` failed the
same way.

**Fixed.** `kicad-skip` is gone from `tools/project.py`. `_summarize` counts
with `sch_editor.iter_instance_symbols`, `pcb_editor.iter_footprints` and
`pcb_editor.list_nets`; `list_components` and `_component_dict` read the parsed
tree through `sch_io`. One behaviour changed: a blank project now reports
`nets: 0` rather than 1, because the unconnected net is not a net.

Verified on the round-2 test board: `set_project` returns
`symbols: 11, footprints: 6, nets: 5`.

## P6 — `update_pcb_from_schematic` was netlist-only (Major) — DONE

The tool exported the netlist and assigned pad nets through `pcbnew`. It did
not place footprints (it listed them in `missing_in_pcb` and expected the
caller to call `add_footprint` once per component, retyping the lib_id the
symbol already carried), did not remove footprints whose symbol was gone, and
did not copy any field — DRC schematic parity on the test board reported 6 ×
`footprint_symbol_field_mismatch`. Its docstring also pointed at
`remove_footprint`, which did not exist.

**Built.** `tools/sync_components.py` does the component side, and
`update_pcb_from_schematic` gained three switches:

- `place_missing=True` — a footprint for every symbol that has none on the
  board, from the symbol's own `Footprint` property, dropped in a grid just
  below the board outline. A symbol with no `Footprint` is reported in
  `no_footprint`; a lib_id the index does not know in `unresolved`. Neither
  stops the rest of the update.
- `sync_fields=True` — `Value` and the symbol's other fields (`MPN`,
  `Manufacturer`, …) copied onto the matching footprint, hidden on `F.Fab`,
  which is what schematic parity compares and what an assembly BOM taken from
  the board needs. Idempotent: a second pass writes nothing.
- `remove_extra=False` — footprints whose symbol is gone are only listed in
  `orphan_footprints` unless this is on. A board legitimately carries
  footprints no symbol knows about (mounting holes, fiducials, logos), and
  losing those to a sync would be worse than leaving a stale part behind.

`remove_footprint(reference)` now exists as a tool as well. And
`place_footprints_grid` gained `only_unplaced` (default True, the old
behaviour): the new placement puts parts below the outline rather than at
(0, 0), so arranging them needs `only_unplaced=False`.

Verified: a three-part schematic goes from a blank board to placed, net-assigned
footprints in one call, with DRC reporting 0 violations and 0 parity findings;
deleting a symbol and re-running with `remove_extra=True` drops its footprint.

## P7 — `export_pcb_svg()` failed with default arguments (Minor) — DONE

`layers` defaulted to `None`, and `kicad-cli pcb export svg` then exited 1 with
`At least one layer must be specified`.

**Fixed.** `kicad_cli.DEFAULT_SVG_LAYERS` — both copper layers, both
silkscreens, both solder masks and `Edge.Cuts` — is used when the caller names
none, and the docstrings say so.

## P8 — the fab package's BOM had no sourcing fields (Minor) — DONE

`export_fab_package` called `export_bom` with the default field set, so
`fab/<project>-bom.csv` was `Refs,Value,Footprint,Qty,DNP`. The `MPN` and
`Manufacturer` properties the sourcing tools write never reached it, and the
package was not ready for assembly quoting.

**Fixed.** `manufacturing.DEFAULT_BOM_FIELDS` is
`Reference,Value,Footprint,MPN,Manufacturer,Datasheet,${QUANTITY},${DNP}`, and
`export_fab_package` takes `bom_fields` to override it. The docstring says that
the call overwrites `fab/<project>-bom.csv`, so a BOM with other columns wants
a path of its own.

## P9 — `create_symbol` could not set arbitrary fields (Minor) — DONE

`create_symbol` took `value`, `footprint`, `datasheet`, `description` and
`keywords` only. A library symbol could not be given `MPN`, `Manufacturer` or
`LCSC`; those could be added only to a placed instance. For a house library
that is the wrong way round — the part number belongs to the library symbol.

**Built.** `create_symbol(fields={"MPN": "...", "Manufacturer": "..."})` writes
each field as a hidden property on the library symbol, so every placement
inherits it. A field that has its own argument (`Footprint`, `Datasheet`, …) is
refused rather than written twice.

## P10 — `remove_wire` could not remove an off-grid wire (Minor) — DONE

`remove_wire` snapped the coordinates it was given before looking, so a wire
drawn with `snap_to_grid=False` — the normal case when connecting to pins that
are not on the 1.27 mm grid — could not be removed:

```
remove_wire(200, 140, 210, 140)
-> KeyError: no wire between (199.39, 139.7) and (209.55, 139.7)
```

**Fixed.** `_candidate_points` tries the coordinates exactly as given first and
the snapped point only as a fallback, in `remove_wire`, `remove_junction` and
`remove_no_connect`. The error now lists every point that was tried.

## P11 — malformed `pins` / `pads` raised a bare `KeyError` (Minor) — DONE

`create_symbol(pins=[{"number": "1", "name": "VIN", "side": "left"}])` raised
`KeyError: 'x_mm'`, naming neither the offending pin nor the expected schema.
`create_footprint` failed the same way.

**Fixed.** `_check_keys` validates every pin and pad dict before it reaches the
node builders:

```
pins[0] is missing x_mm, y_mm; a pin takes {number, name, x_mm, y_mm,
length_mm, angle_deg, angle, type, shape} (required: number, x_mm, y_mm)
```

An unknown key is refused too, rather than silently leaving the value at its
default — `{"pin_type": "power_in"}` used to produce a passive pin without a
word.

## P12 — autorouting was not verified (Info) — DONE

`autoroute_pcb` raised `FreeroutingError: freerouting.jar not found`, so only
`export_dsn` had been exercised (8 kB DSN written from the test board).

The jar was then placed at `third_party/freerouting-2.1.0.jar` — the name the
releases download under — and `find_freerouting_jar` only ever looked for the
exact name `freerouting.jar`, so it was still not found.

**Fixed.** `find_freerouting_jar` falls back to `third_party/freerouting*.jar`,
newest version first, after `FREEROUTING_JAR` and the unversioned name.

Verified end to end on the round-2 test board with Freerouting 2.1.0 and
JDK 21: 6 unrouted connections before, 0 after, and `run_drc(refill_zones=True)`
reports 0 errors, 0 unconnected items and 0 parity findings — only two
`silk_overlap` warnings from the scratch placement.

## P13 — the tests encoded the old formats (Major, test debt) — DONE

The fast suite passed with P1 and P2 both live: the ERC unit test fed
`_shape_erc` a hand-written report with a flat `violations` list, a shape KiCad
10 does not produce, and the acceptance test asserted `total_violations >= 0`,
which cannot fail. The PCB fixtures all carried a legacy net table, so the net
tools were never exercised against a board `pcbnew` wrote.

**Built.** The suite is 425 fast tests (was 390) and 43 slow ones, with:

- `test_phase7_validation.py` — `_shape_erc` against a KiCad 10 per-sheet
  report and against a flat one, plus a slow test that builds a schematic with
  a dangling wire and requires `run_erc` to report it. That test fails against
  the old shaping.
- `test_phase17_pcb_connectivity.py` — a KiCad 10 board fixture (no net table,
  pads named by net) covering `list_nets`, `list_unrouted`, writing a track by
  name, refusing an unknown net, and a zone written as `(net "GND")`; plus the
  legacy-board counterparts, so both formats stay covered.
- `test_phase2_indexer.py` — dotted Windows version directories, the refusal to
  save an empty index, and the project-library overlay.
- `test_phase1_project.py` — the summary on a board with a footprint on it.
- `test_phase10_rules_sync.py` — the component sync: placement, the two
  reporting paths, field copying and idempotence, orphan listing vs removal.
- `test_phase3_schematic.py`, `test_phase5_pcb.py`, `test_phase9_manufacturing.py`,
  `test_phase13_advanced.py` — the P7–P11 behaviours.

---

## P14 — `add_power_symbol("PWR_FLAG")` got a `#PWR` reference (Minor) — DONE

Every part from the `power` library was given a `#PWR####` designator,
including `PWR_FLAG`, which KiCAD designates `#FLG`. Reproduced:

```
add_power_symbol('GND')      -> '#PWR0001'
add_power_symbol('+3V3')     -> '#PWR0002'
add_power_symbol('PWR_FLAG') -> '#PWR0003'   <- should be #FLG0001
```

**Cause.** `tools/schematic.py::_next_power_reference` hardcoded the prefix: it
scanned `#PWR0*(\d+)` and returned `f"#PWR{n:04d}"` whatever part was being
placed. The prefix is in the library part's own `Reference` property — `#FLG`
for `PWR_FLAG`, `#PWR` for `GND` and the rails — and nothing read it.

**Impact, measured.** Minor. ERC does not object: a sheet with the wrong prefix
gives the same violations as one without. Connectivity is unaffected, because
`sch_netlist._is_power_symbol` already accepted both prefixes. What it cost was
diff churn — KiCAD's own annotation assigns `#FLG` and would renumber our flags
— and flags consuming `#PWR` numbers.

**Built.** `_library_reference_prefix` reads the prefix off the library part,
and `_next_virtual_reference(tree, prefix)` numbers each prefix independently,
as KiCAD does. `_next_power_reference` stays as the `#PWR` alias so existing
callers and tests keep working. Now:

```
GND -> #PWR0001, +3V3 -> #PWR0002, PWR_FLAG -> #FLG0001,
PWR_FLAG -> #FLG0002, GND -> #PWR0003
```

---

## P15 — a sheet's cached `lib_symbols` could not be refreshed (Major) — DONE

A sheet carries its own copy of every library part it places, in
`(lib_symbols ...)`, so it opens without the libraries. Nothing updated that
copy when the library changed.

**Worse than a missing tool.** `sch_editor.inject_lib_symbol` returns early
when the `lib_id` is already cached, so a symbol placed *after* a library
change still got the old geometry:

```
pins as placed:         [('1', (100.33, 97.79)), ('2', (100.33, 102.87))]
library edited on disk (pin 1 moved 2.54 -> 7.62)
pins after lib edit:    [('1', (100.33, 97.79)), ...]   <- stale, expected
a NEW placement's pins: [('1', (150.0,  97.79)), ...]   <- stale, not expected
```

Everything downstream reads the cache — `pins_of_instance`, hence
`list_sch_nets`, `trace_net`, `find_dangling` and `get_pin_position` — so wires
were placed against coordinates the library no longer used. `replace_symbol`
had the same hole when swapping to an already-cached `lib_id`.

**No shortcut existed.** `kicad-cli sch` offers only `erc`, `export` and
`upgrade`. KiCAD does this in the GUI through **Tools → Update Symbols from
Library** (`SCH_ACTIONS::updateSymbolFields`), whose dialog has per-field
checkboxes — because overwriting `Value` and `Footprint` on every placement
undoes the board's part choices.

**Built.** Two tools, and the adapter functions under them
(`replace_lib_symbol`, `lib_symbol_ids`, `diff_lib_symbol`,
`refresh_lib_symbol`):

- `list_lib_symbols(scope)` — what each sheet has cached, with `stale` and the
  pins added, removed or moved against the library on disk.
- `refresh_lib_symbols(lib_id="", scope, update_fields=None, dry_run=True)` —
  re-copies the definitions.

Three deliberate choices:

- **`dry_run=True` is the default.** A refresh can orphan wiring, so the first
  call reports and writes nothing.
- **`connections_at_risk`** names, per placement, each pin that would move or
  disappear while carrying a wire, junction, no-connect or label.
  `find_dangling` is the after-check: in the test, the orphaned wire shows up
  as a `wire_end` at the pin's old position.
- **No field is copied onto placements unless named.** `Reference`, `Value` and
  `Footprint` belong to the placement (`ed.INSTANCE_OWNED_FIELDS`). Naming one
  in `update_fields` is allowed, but has to be explicit.

The cached entry is replaced in place, so the block keeps its order and the
diff stays small.

**Tests.** New `tests/test_phase19_lib_symbol_refresh.py`, 27 tests including
the two staleness demonstrations above and a `kicad-cli sch erc` load check
after the block is rewritten.

---

## Verification

```bash
uv run pytest -m "not slow and not network" -q   # 428 passed
uv run pytest -m "slow" -q                       # 46 passed
uv run ruff check .                              # 47 pre-existing findings, unchanged
```

P14 and P15 were verified afterwards:

```bash
uv run pytest -m "not network" -q                # 508 passed
uv run ruff check .                              # 47, unchanged
```

The end-to-end flow was re-run afterwards with no environment configuration at
all — no `KICAD_LIBRARY_PATH` — from `create_project` through custom symbol and
footprint creation, wiring, annotation, ERC, `update_pcb_from_schematic`,
ground plane, DRC, STEP, render, SVG and `export_fab_package`. Every tool
returned; ERC reported the circuit's two real power-pin errors, `list_unrouted`
agreed with DRC's unconnected count, and schematic parity was clean.
