# Open points — round 2

Gathered on 2026-09-19 from an end-to-end run of the design flow the server
claims to support: create a project, create a symbol and a footprint, place and
wire a schematic, fill the sourcing fields, annotate, run ERC, push the netlist
to the PCB, place and route, review, then export 3D and fabrication outputs.

The run built a real two-layer board (LDO + two caps + LED + resistor + 2-pin
header) in a scratch directory and drove every tool through the same
`register(mcp)` entry points the MCP server uses. KiCad 10.0.6, board format
`20260206`, schematic format `20260306`.

**What works.** Project creation, symbol and footprint creation, symbol
placement with autoplaced fields, arbitrary symbol properties (`MPN`,
`Manufacturer`, ...), wires, junctions, power symbols, labels, annotation of
`?` references, `list_sch_nets` / `trace_net` / `find_dangling`, board outline,
layer count, footprint placement and moves, `validate_decoupling_caps`, DRC
(including schematic parity), Gerbers, drill, position file, BOM, netlist,
STEP, 3D render, `export_fab_package`, backups through `safe_write`, and
formatting preservation — a symbol added and then removed leaves a
byte-identical file, and every write produced a minimal diff.

**What does not.** The list below. P1 and P2 are the serious ones: ERC always
reports a clean schematic, and every net-aware PCB tool misreads boards written
by KiCad 10.

---

## P1 — `run_erc` always reports zero violations (Critical)

`adapters/kicad_cli.py:142` `_shape_erc` reads `data["violations"]`. KiCad 10's
ERC JSON (`https://schemas.kicad.org/erc.v1.json`) has no top-level
`violations` key — violations are nested per sheet:

```
{"source": ..., "sheets": [{"path": "/", "uuid_path": ..., "violations": [...]}]}
```

The key is therefore always missing, and the tool reports
`errors: 0, warnings: 0, total_violations: 0` for every schematic.

Measured on the test board, with an unconnected LM358 and a dangling wire in
place:

- `run_erc()` returned `{"errors": 0, "warnings": 0, "total_violations": 0}`.
- `kicad-cli sch erc --severity-all` on the same file found 6 violations:
  2 x `power_pin_not_driven` (error), 1 x `wire_dangling` (error),
  1 x `endpoint_off_grid`, 2 x `unconnected_wire_endpoint` (warnings).

The MCP call even wrote the correct raw report to `e2e.erc.json`; only the
shaping dropped it. This is the worst defect found: the project's own rule is
to prove a write valid with `kicad-cli`, and ERC currently certifies every
schematic as clean.

`run_drc` is unaffected — the DRC schema does keep `violations` at the top
level — but it should be re-checked against the schema rather than assumed.

Fix: walk `data["sheets"][*]["violations"]`, keep a per-sheet breakdown, and
fall back to the flat key for older reports.

## P2 — the PCB net model is the pre-KiCad-10 one (Critical)

KiCad 10 (board `20260206`) no longer writes a top-level
`(net <index> "<name>")` table. Pads, zones and tracks carry the net **by
name**: `(pad "1" smd rect ... (net "GND"))`, `(zone (net "GND") ...)`. A board
saved by `pcbnew` in this run contains zero top-level `(net ...)` declarations.

`adapters/pcb_editor.list_nets` (and `find_net_index`) only recognise
`(net <int> "<name>")`, so on a KiCad 10 board they see nothing. Everything
built on them degrades silently:

| Tool | Observed on the test board | Truth |
|---|---|---|
| `list_nets` | `{"nets": []}` | 4 nets, 13 pads assigned |
| `net_route_status(net_name="GND")` | `KeyError: no net named 'GND' on this board` | GND exists |
| `list_unrouted` | `count: 0` ("fully routed") | DRC: 9 unconnected items |
| `get_pad_position` | `net_number: 0` for `/+3V3` | pcbnew reports netcode 3 |
| `analyze_ground_coverage` | `"no GND zone on B.Cu"` | a GND zone is on B.Cu |

Two writers make it worse:

- `add_zone` writes the legacy pair `(net 0)` + `(net_name "GND")` and appends
  a fabricated top-level `(net 0 "GND")`. Index 0 is reserved for the
  unconnected net. KiCad still resolves the zone to GND through `net_name`, so
  the board is not corrupt, but the declaration is wrong, and it is what makes
  `list_nets` return a single bogus entry on boards this server wrote.
- `add_track(net=...)` takes an integer net index. On a KiCad 10 board no such
  index exists in the file; the call accepts any number and writes it
  unchecked.

Fix: make net identity a name everywhere — read the set of nets from pad, zone
and track `(net ...)` nodes, accept a name in `add_track` / `add_via`, and
write zones as `(net "<name>")`. Keep reading the legacy table so older boards
still load.

Blast radius beyond the table above: `add_via`, `add_ground_stitching`,
`add_via_array`, `add_rf_microstrip`, the diff-pair tools and
`check_return_path_continuity` resolve nets the same way and were not
exercised individually.

## P3 — Windows library auto-detection never finds KiCad (Major)

`utils/kicad_paths._KICAD_VERSIONS = ("10", "9", "8", "7")` builds
`C:\Program Files\KiCad\10\share\kicad\symbols`. The installer uses `10.0`,
`7.0`, ... — with a dot. `find_symbol_lib_dirs()` and
`find_footprint_lib_dirs()` therefore return `[]` on a stock Windows install,
and only a `KICAD_LIBRARY_PATH` entry hides it. The `.env` in this repo has the
variable present but empty, so a fresh checkout indexes nothing.

The failure is silent and destructive: `index_libraries(force=True)` with no
directories found builds an empty index and **overwrites**
`~/.cache/kicad-claude/index.json` with `0 symbols, 0 footprints`. That
happened during this run and had to be repaired by hand.

Fix: probe `major.minor` directory names (glob `C:\Program Files\KiCad\*`), and
refuse to save an index that found no libraries — return an error naming the
directories that were searched.

## P4 — libraries the server creates are not usable by the server (Major)

`create_symbol` writes `<project>/lib/<Lib>.kicad_sym` and registers it in the
project's `sym-lib-table`; `create_footprint` does the same for
`<project>/lib/<Lib>.pretty` and `fp-lib-table`. Neither path is ever indexed:
`indexer.build_index` walks the global directories only, and reads neither
`sym-lib-table` / `fp-lib-table` nor the active project's `lib/`.

`add_symbol` and `add_footprint` resolve a `lib_id` exclusively through the
index, so the sequence the tools advertise fails:

```
create_symbol("E2ELib", "MYREG", ...)  -> OK
index_libraries(force=True)            -> 224 libs, 22874 symbols (E2ELib absent)
add_symbol("E2ELib:MYREG", "U1", ...)  -> KeyError: unknown lib_id 'E2ELib:MYREG'
```

Adding `<project>/lib` to `KICAD_LIBRARY_PATH` makes the same sequence work, so
the gap is indexing scope only. `import_vendor_zip` lands in the same place and
has the same problem.

Fix: include the active project's `lib/` — better, the directories listed in
its `sym-lib-table` / `fp-lib-table` — in the index, and re-index the project
library automatically after `create_symbol`, `create_footprint` and
`import_vendor_zip`.

## P5 — `set_project` raises on any project with a populated PCB (Major)

`tools/project.py:15` imports `PCB` and `Schematic` from `kicad-skip`, used by
`_summarize` for the symbol / footprint / net counts. `kicad-skip` cannot parse
a KiCad 10 footprint's text items:

```
File ".../skip/sexp/parser.py", line 618, in __init__
    for i in pv._base_coords:
AttributeError: 'Symbol' object has no attribute '_base_coords'
```

So `set_project` raises on a project whose `.kicad_pcb` holds at least one
footprint — that is, on any real project. `state.set_active` runs first, so the
project *is* active and the following calls work, which makes the error look
random. `get_project_state` and the summary returned by `create_project` fail
the same way. `_summarize` also reads `pcb.net`, which is P2 again.

Fix: drop `kicad-skip` from the summary and count with the project's own
`sch_io` / `pcb_editor` readers, which parse these files correctly.

## P6 — `update_pcb_from_schematic` is netlist-only (Major)

The tool exports the netlist and assigns pad nets through `pcbnew`. It does
not:

- add footprints for schematic symbols missing from the PCB — it lists them in
  `missing_in_pcb` and expects the caller to call `add_footprint` once per
  component, with the `lib_id` retyped by hand instead of read from the
  symbol's `Footprint` property;
- remove footprints whose symbol is gone;
- update a footprint's value, or copy the symbol's fields. DRC schematic parity
  on the test board reported 6 x `footprint_symbol_field_mismatch` (`MPN`
  missing on four footprints, `Datasheet` and `Description` differing).

For "update the PCB from the schematic" to mean what it says, the tool should
at least place the missing footprints from each symbol's `Footprint` property
and carry the fields across. Until then the docstring should say plainly that
the caller must place footprints first.

Also: the docstring points at `remove_footprint`, which does not exist — the
server has no tool to delete a footprint from a board.

## P7 — `export_pcb_svg()` fails with default arguments (Minor)

`layers` defaults to `None`, and `kicad-cli pcb export svg` then exits 1 with
`At least one layer must be specified`. Either default to the usual set
(`F.Cu,B.Cu,F.Silkscreen,B.Silkscreen,Edge.Cuts`) or make the argument required
with a message that names the format.

## P8 — the fab package's BOM has no sourcing fields (Minor)

`export_fab_package` calls `export_bom` with the default field set, so
`fab/<project>-bom.csv` is `Refs,Value,Footprint,Qty,DNP`. The `MPN` and
`Manufacturer` properties the sourcing tools write never reach it, and the
package is not ready for assembly quoting. `export_bom(fields=...)` works when
called directly — the fields are passed through correctly.

Worth knowing as well: `export_fab_package` overwrites
`fab/<project>-bom.csv`, so a BOM exported earlier with custom fields is lost.

## P9 — `create_symbol` cannot set arbitrary fields (Minor)

`create_symbol` takes `value`, `footprint`, `datasheet`, `description` and
`keywords` only. A library symbol cannot be given `MPN`, `Manufacturer`, `LCSC`
or any other property; those can be added only to a placed instance, via
`set_symbol_property`. For a house library that is the wrong way round — the
part number belongs to the library symbol.

## P10 — `remove_wire` cannot remove an off-grid wire (Minor)

`add_wire` has `snap_to_grid`; `remove_wire` does not, and always snaps the
coordinates it is given. A wire drawn with `snap_to_grid=False` — the normal
case when connecting to pins that are not on the 1.27 mm grid — cannot be
removed:

```
remove_wire(200, 140, 210, 140)
-> KeyError: no wire between (199.39, 139.7) and (209.55, 139.7)
```

`remove_junction` and `remove_no_connect` should be checked for the same thing.

## P11 — malformed `pins` / `pads` raise a bare `KeyError` (Minor)

`create_symbol(pins=[{"number": "1", "name": "VIN", "side": "left"}])` raises
`KeyError: 'x_mm'`, naming neither the offending pin nor the expected schema.
`create_footprint` fails the same way. Validate the dicts and say which entry
is wrong.

## P12 — autorouting not verified (Info)

`autoroute_pcb` raises `FreeroutingError: freerouting.jar not found` on this
machine; `FREEROUTING_JAR` is empty and `third_party/freerouting.jar` is
absent. `export_dsn` works (8 kB DSN written from the test board), so only the
Freerouting leg is untested. Not a code defect — it needs the jar and a JRE.

## P13 — the tests encode the old formats (Major, test debt)

`uv run pytest -m "not slow and not network"` is 390 passed, 1 skipped — with
P1 and P2 both live. Two reasons:

- `tests/test_phase7_validation.py:36` feeds `_shape_erc` a hand-written report
  with a flat `violations` list. KiCad 10 does not produce that shape, so the
  test passes while the tool is blind. The acceptance test at line 152 asserts
  `total_violations >= 0`, which cannot fail.
- The PCB fixtures still carry a legacy top-level net table, so the net tools
  are never exercised against a board `pcbnew` wrote.

Fix alongside P1 and P2: generate the ERC and DRC fixtures with `kicad-cli`
from a schematic with known violations, and add a PCB fixture saved by KiCad 10
(no net table, named pad nets).

---

## Suggested order

1. P1 — one function; restores the ability to prove a schematic is valid.
2. P2 — the net model; touches `pcb_editor` and every net-aware tool.
3. P13 — fixtures, so 1 and 2 stay fixed.
4. P5, P3, P4 — the flow is unusable on a clean machine without them.
5. P6 — makes `update_pcb_from_schematic` match its name.
6. P7-P11 — small and independent.
