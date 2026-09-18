# Open points

Everything still outstanding across the project's documents, gathered into one
list on 2026-09-18. Sources:

- `docs/kicad_mcp_issues.md` — the Firbox field report (issues 1–10).
- `docs/issue8_formatting_probe.md` — the formatting measurement.
- `docs/plan_phase16_connectivity_and_edit_tools.md` — points 2–5, all built.

Each point carries its origin, so the source document stays readable on its own.
Numbering is independent of the source: P1 is not issue 1.

Severity follows the field report's scale:

- **Blocker** — corrupts files or makes a tool unusable.
- **Major** — produces ERC problems or forces manual GUI work.
- **Minor** — cosmetic or ergonomic.

No blockers are open.

---

## P1 — `replace_symbol` is missing (Major)

*From issue 6, suggestion list.*

Swapping a part means removing the symbol and adding the replacement, which
loses its position and every wire that reached its pins. There is no tool that
keeps them.

**Wanted.** `replace_symbol(reference, lib_id, pin_map)` — swap the library
symbol while keeping position, rotation and reference, and reconnect the pins
named in `pin_map`. Pins with no mapping leave their wires dangling and should
be reported in the result rather than silently dropped.

**Notes.** The rest of issue 6 is closed: `remove_wire`,
`remove_items_in_box`, `add_junction`/`remove_junction`,
`set_symbol_property`, `set_dnp`/`set_in_bom`/`set_on_board`, `rename_label`,
`add_text`/`set_text`/`remove_text`, `move_item` and
`remove_symbol(remove_connected_wires=True)` all exist.

`find_dangling` (phase 16) now reports what a bad swap would leave behind, so
this is testable in a way it was not when the issue was filed.

---

## P2 — A placed symbol's fields are not autoplaced (Minor)

*From issue 7.*

`add_symbol` and `add_power_symbol` put `Reference` and `Value` text at the
symbol origin, so `#PWR0001` overlaps the `GND` graphic and the part name. The
GUI's "Autoplace Fields" fixes it by hand.

**Wanted.** Place `Reference` and `Value` at the library symbol's own field
offsets, rotated with the instance, or emulate KiCAD's autoplace for simple
two-pin parts. Set `(fields_autoplaced yes)` only when that is genuinely what
was done.

**Notes.** `sch_editor` already writes `(fields_autoplaced yes)` on a `(sheet
...)`, but never on a `(symbol ...)`. `move_item` now drags a symbol's property
fields with the symbol, so the offsets survive a move once they are right.

---

## P3 — Tool results do not report post-write validity (Minor)

*From issue 10.*

Nothing checks that a written file still loads. Issue 1 (unescaped newlines)
corrupted a whole hierarchy and went unnoticed until `kicad-cli` was run by
hand; a cheap guard would have caught it on the first write.

**Wanted.** After each write, re-parse the file. Where `kicad-cli` is
available, optionally run a fast load check — `kicad-cli sch export netlist -o
<tmp>` — and return `"load_ok": false` with stderr when it fails, offering the
backup that was just written.

**Notes.** Make it opt-in or cached; `kicad-cli` takes seconds, and paying that
on every `add_wire` would make bulk edits unusable. `find_dangling` and
`list_unrouted` now give a fast structural check that needs no subprocess, which
covers part of the intent.

---

## P4 — `.backups/` grows without limit (Minor)

*From issue 9.*

Every mutating call copies the whole sheet to `<project>/.backups/`. One
session produced 88 files.

**Wanted.** Keep the last N backups per file, or one per session. Storing them
in the user cache directory instead is the other option the issue raised.

**Notes.** Partly addressed: `.backups/` is in `.gitignore`, so they no longer
show up as untracked. The growth itself is untouched —
`sch_editor.backup_file` writes a new timestamped copy every time and prunes
nothing.

---

## P5 — No check that KiCAD has the project open (Minor)

*From the field report's closing notes.*

KiCAD must be closed while the tools mutate files, and nothing enforces it. A
mutating call against a project open in the GUI can be overwritten by KiCAD's
own save, or corrupt what the GUI holds.

**Wanted.** Check for the lock file `~<project>.kicad_pro.lck` in every
mutating tool and return a clear error naming it. An override flag is worth
having for the case where a stale lock survived a crash.

---

## P6 — `(members ...)` wrapping does not match KiCAD (Minor)

*From `docs/issue8_formatting_probe.md`, "Known remaining deviation".*

12 of KiCAD 10's 78 demo schematics still differ after parse → dump, all of
them only in how `(members ...)` wraps inside `(bus_alias ...)`. KiCAD breaks
those lines earlier than a 118-column budget explains, and inconsistently
between files: 92, 91 and 93 columns in `flash`, `vme_interface` and `ddr4-ps`,
while `peripherals` wraps where the next item would reach only column 78.

**Wanted.** Either find the rule KiCAD actually uses, or leave it. It affects
bus aliases only, and KiCAD loads the result either way.

**Notes.** Low value. A project with no bus aliases never sees it. Worth
revisiting only if a real diff gets noisy because of it.

---

## P7 — `search_symbol` results are not brace-decoded (Minor)

*From the brace-escape work, 2026-09-18.*

`list_components`, `list_nets`, `list_sheets` and `list_pins` decode KiCAD's
`{brace}` escapes before showing a name, so a net stored `VBUS{slash}5V` reads
as `VBUS/5V`. `search_symbol` does not.

It was left alone deliberately: its results carry `lib_id`, which callers pass
straight back to `get_symbol_details` and `add_symbol`, and which
`lib_symbols_has` compares exactly against the tree. Decoding it would break
identity lookup — a worse bug than the display one.

**Wanted.** Decode the human-readable fields (`description`, `keywords`) while
leaving `lib_id` and `name` in their stored form. Small, but it needs the
result shape to distinguish "for reading" from "for passing back", which it
does not today.

---

## Not server bugs

Carried over from the field report's closing notes, kept here so they are not
re-investigated.

- **KiCAD library symbol.** `Regulator_Linear:TPS7A4701xRGW` omits NC pins 2
  and 17–19. Harmless, but check `get_symbol_details` pin lists against the
  datasheet before wiring.

---

## Closed since the field report

For reference, so a reader of `docs/kicad_mcp_issues.md` knows what not to
re-open. Each is marked in that document too.

| Issue | Subject | Where it was fixed |
|---|---|---|
| 1 | Unescaped newlines made schematics unloadable | `sch_io._escape` now escapes `\\`, `"`, `\n`, `\r` |
| 2 | Y-flip put every item off-grid | PCB moved to KiCAD-native Y-down; `pcb_to_file_xy` is the identity |
| 3 | `run_erc` could not find `kicad-cli` on Windows | `find_kicad_cli` honours `KICAD_CLI`, then PATH, then per-OS install paths |
| 4 | `list_components` ignored the active sheet | `scope="active"` / `"all"` |
| 5 | `#PWR` references duplicated across sheets | `all_references_in_hierarchy` |
| 6 | Missing edit primitives | Phase 3 and plan points 3 and 5 — except P1 above |
| 8 | Whole-file reformat on every write | Formatting matched to KiCAD 10 (66/78 demos byte-identical), then line endings preserved — except P6 above |
| 9 | `.backups/` untracked in git | `.gitignore` — growth is P4 above |
