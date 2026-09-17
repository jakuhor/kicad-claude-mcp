# kicad-claude-mcp — issues found in Firbox use

Field report from schematic editing on the Firbox base board
(`hardware/base_board`, KiCad 10.0.5/10.0.6, Windows 11), 2026-09-16/17.
Server: `Greg3001/kicad-claude-mcp` at commit `19fc150`
(`fix(library_create): write bare symbol names in source .kicad_sym files`).

Each issue lists what happened, how to reproduce it, the root cause where it was
traced in the source, and a suggested fix. Severity:

- **Blocker** — corrupts files or makes a tool unusable.
- **Major** — produces ERC problems or forces manual GUI work.
- **Minor** — cosmetic or ergonomic.

---

## 1. Newlines in string literals are written unescaped — schematic no longer loads (Blocker)

**Observed.** After any MCP write to a sheet, text items that contained `\n`
(e.g. `(text "TODO:\n+ USB PD\n- EMI filter design ...")`) were re-serialised
with literal line breaks inside the quoted string. `kicad-cli` then refused the
whole hierarchy:

```
Failed to load schematic
```

(`kicad-cli sch export netlist` and `kicad-cli sch erc` both exit with code 3.)

**Repro.**
1. Root sheet contains a text item with `\n` escapes.
2. Call any mutating tool on that sheet, e.g. `add_symbol` or `add_wire`.
3. Run `kicad-cli sch erc base_board.kicad_sch`.

**Root cause.** `src/kicad_claude/adapters/sch_io.py`: `sexpdata.loads` decodes
`\n` into a real newline, but `_escape()` only escapes `\\` and `"`:

```python
def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
```

**Suggested fix.** Also escape control characters (at least `\n`, `\r`, `\t`)
when writing, and add a round-trip test (parse → dump → `kicad-cli` load) using
a fixture with multi-line text, multi-line property values and quotes.

---

## 2. Y-axis flip puts every placed item 0.18 mm off-grid on A3 sheets (Major)

**Observed.** On an A3 sheet, every item placed through the MCP landed 0.18 mm
off the 1.27 mm (50 mil) grid, even when the MCP coordinates were grid multiples.
ERC reported `endpoint_off_grid` for every new pin, wire and power symbol. The
GUI "Align to grid" step was needed afterwards.

Example: `add_symbol(..., x_mm=228.6, y_mm=147.32)` was written as
`(at 228.6 149.68 0)`. 149.68 is not a multiple of 1.27; 149.86 is.

**Root cause.** `utils/geometry.py`:

```python
def mcp_to_kicad_xy(x_mm, y_mm, page_height_mm=DEFAULT_PAGE_HEIGHT_MM):
    return float(x_mm), float(page_height_mm) - float(y_mm)
```

The page height is taken from `(paper "A3")` = 297 mm, and 297 mm is not a
multiple of 1.27 mm. Grid-aligned MCP Y values therefore map to off-grid file Y
values. A4 (210 mm) has the same problem.

**Workaround used.** Choose MCP Y values of the form `297 − 1.27·n`, or use pin
positions returned by `list_pins` of an already on-grid symbol as anchors.

**Suggested fix, in order of preference.**
1. Flip around a grid-aligned height, e.g. `ceil(page_h / 1.27) * 1.27`, so a
   grid point always maps to a grid point.
2. Or offer a schematic mode with no flip (KiCad-native Y-down), since schematic
   users read coordinates off the KiCad GUI.
3. Optionally, snap to the grid by default (`snap_to_grid: bool = True`) and
   report the snapped coordinates in the response.

---

## 3. `run_erc` cannot find `kicad-cli` on Windows (Major)

**Observed.**

```
Error executing tool run_erc: `kicad-cli` not found on PATH and not in the
standard install path. Add it to PATH (macOS: /Applications/KiCad/KiCad.app/Contents/MacOS)
or set KICAD_CLI in the environment.
```

`kicad-cli` exists at `C:\Program Files\KiCad\10.0\bin\kicad-cli.exe`.

**Root cause.** `utils/kicad_paths.py::find_kicad_cli()` checks only
`shutil.which("kicad-cli")` and the macOS bundle path. It ignores the
`KICAD_CLI` environment variable that the error message tells the user to set,
and it has no Windows or Linux default install paths.

**Suggested fix.**
- Honour `KICAD_CLI` first.
- Probe `%ProgramFiles%\KiCad\<version>\bin\kicad-cli.exe`, newest version
  first. Several versions can coexist: this machine has 7.0 and 10.0, and 10.0
  must be used.
- Probe `/usr/bin/kicad-cli` on Linux.
- Report which binary and version were used in the tool result.

---

## 4. `list_components` ignores the active sheet (Major)

**Observed.** After `set_active_sheet("400_symetric_analog.kicad_sch")`,
`list_components` still returned the root sheet's symbols. `add_symbol`,
`remove_symbol` and `list_pins` did honour the active sheet, so the tools are
inconsistent. This made it hard to confirm which sheet an edit would hit.

**Root cause.** `tools/project.py::list_components()` always opens
`proj.sch_path` (the root schematic):

```python
sch = Schematic(str(proj.sch_path))
```

**Suggested fix.** Use the active sheet path. Optionally add a
`scope: "active" | "all"` parameter, where `all` walks the hierarchy and tags
each entry with its sheet path.

---

## 5. `#PWR` references duplicate across sheets (Major)

**Observed.** `add_power_symbol` on the root sheet produced `#PWR0001…#PWR0009`,
which were already used on sub-sheet `400_symetric_analog`. `kicad-cli` then
warned:

```
Warning: schematic has annotation errors, please use the schematic editor to fix them
```

**Root cause.** `tools/schematic.py::_next_power_reference(tree)` scans only the
current sheet's tree for used numbers.

**Suggested fix.** Collect references across the whole hierarchy (root and all
sub-sheets, including per-instance references in `(instances ...)`) before
picking the next free number. The same applies to any auto-numbering of normal
references.

---

## 6. Missing edit primitives force hand edits of `.kicad_sch` (Major)

The tool set can add wires, labels and symbols, but cannot modify or delete most
of them. During one session, these operations had no tool:

| Needed operation | Workaround used |
|---|---|
| Delete wire / junction / no-connect | Manual cleanup in KiCad GUI (a scripted S-expression delete was blocked by the agent's permission policy) |
| Add junction | Avoided T-joints; split wires so endpoints coincide |
| Change a symbol property (Value, Footprint, DNP, MPN) | Hand `Edit` of `(property "Value" ...)` |
| Rename a label | Hand `Edit` of `(label "+6V" ...)` |
| Add or edit a text note | Hand `Edit` of `(text ...)` |
| Move a wire, label or no-connect | Not done; GUI "Align to grid" |
| Autoplace symbol fields | Not done; GUI "Autoplace Fields" |

Removing a symbol with `remove_symbol` leaves its wires, junctions and
no-connect markers orphaned. That produced about 50 dangling items and 2
`no_connect_dangling` warnings on the Firbox sheet.

**Suggested tools.**
- `remove_wire(x1,y1,x2,y2)` or `remove_items_in_box(x1,y1,x2,y2, kinds=[...])`.
- `add_junction(x,y)` and `remove_junction`.
- `set_symbol_property(reference, name, value)`, plus `set_dnp(reference, bool)`.
- `rename_label(old, new)` or `set_label(x,y,name)`.
- `add_text` / `edit_text` / `remove_text`.
- `remove_symbol(reference, remove_connected_wires: bool = False)` — delete wire
  stubs that end on the removed pins and are no longer connected to anything else.
- `replace_symbol(reference, lib_id, pin_map)` — swap a part while keeping
  position and connectivity where pins map.

---

## 7. Symbols placed without autoplaced fields (Minor)

**Observed.** Reference and value text of `add_symbol` / `add_power_symbol`
results sat on top of the symbol origin (for example `#PWR0001` overlapping `GND`
and the part name). "Autoplace Fields" in the GUI fixed it.

**Suggested fix.** Place `Reference` and `Value` using the library symbol's
default field offsets, rotated with the symbol, or emulate KiCad's autoplace
for simple 2-pin parts. Set `(fields_autoplaced yes)` only when that is true.

---

## 8. Whole-file reformat on every write (Minor)

**Observed.** A handful of symbol and wire additions to
`400_symetric_analog.kicad_sch` gave a git diff of about 6000 lines
(`4002 insertions(+), 2015 deletions(-)`), because the custom pretty-printer
layout differs from KiCad 10's own output. This makes review of MCP edits
impractical.

The exact formatting differences were not analysed.

**Suggested fix.** Match KiCad 10's output formatting exactly. Or do
surgical text-level insertion and deletion instead of re-dumping the whole tree.
A regression test could assert that parse → dump of a KiCad-saved file is
byte-identical.

---

## 9. `.backups/` directory grows unbounded and is not ignored (Minor)

**Observed.** Every mutating call writes a full copy of the sheet to
`<project>/.backups/`. One session produced 88 files there, all showing up as
untracked in git.

**Suggested fix.**
- Keep the last N backups per file, or one backup per session.
- Add `.backups/` to a `.gitignore` the server creates on first write.
- Or store backups in the user cache directory instead.

---

## 10. Tool results don't surface post-write validity (Minor)

Given issue 1, a cheap guard would have caught the corruption immediately.

**Suggested fix.** After each write, optionally re-parse the written file. When
`kicad-cli` is available, run a fast load check such as
`kicad-cli sch export netlist -o <tmp>` and return `"load_ok": false` plus
stderr if it fails. Offer to restore from the backup that was just written.

---

## Notes that are not server bugs

- **KiCad library symbol:** `Regulator_Linear:TPS7A4701xRGW` omits NC pins 2,
  17–19. That is harmless, but `get_symbol_details` pin lists should be checked
  against the datasheet before wiring.
- **KiCad must be closed** while tools mutate files. The project's `CLAUDE.md`
  already documents this. Whether the server checks for the lock file
  (`~<project>.kicad_pro.lck`) was not verified. If it doesn't, a check in every
  mutating tool that returns a clear error would enforce this.
