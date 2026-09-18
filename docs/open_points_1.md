# Open points

Gathered on 2026-09-18 from the project's documents, and worked through the
same day. Sources:

- `docs/kicad_mcp_issues.md` — the Firbox field report (issues 1–10).
- `docs/issue8_formatting_probe.md` — the formatting measurement.
- `docs/plan_phase16_connectivity_and_edit_tools.md` — points 2–5, all built.

**Status: P1–P7 are all closed.** Nothing is outstanding. Each point below
records what was done, so the reasoning survives. Numbering is independent of
the source: P1 is not issue 1.

---

## P1 — `replace_symbol` is missing (Major) — DONE

*From issue 6, suggestion list.*

Swapping a part meant removing the symbol and adding the replacement, losing
its position and every wire that reached its pins.

**Built.** `replace_symbol(reference, lib_id, pin_map=None, value=None)`.

Kept across the swap: reference, position, rotation, mirror, unit, uuid, the
whole `(instances ...)` block, and the dnp / in_bom / on_board flags. Keeping
the uuid is what makes the symbol stay the *same* symbol to KiCAD, so the PCB
keeps its footprint association. Value, Footprint, Datasheet and Description
carry over unless overridden.

`pin_map` maps old pin number to new. Wires, junctions, no-connects and labels
sitting on a mapped pin move to that pin's new position. Omitted, it maps every
pin number the two parts share. The result reports `reconnected`,
`left_dangling` (old pins whose attachments were not carried) and
`new_pins_unconnected` — the plan's requirement that nothing be dropped
silently.

Two details worth knowing:

- A rejected `pin_map` puts the original symbol back before raising, so a bad
  call cannot leave a half-swapped symbol behind.
- Mappings apply in pin-number order and a point is consumed by the first pin
  that claims it. Where two pins of the old part sat on one point — which KiCAD
  allows — only the first mapping moves what was there.

Tests use a new `MiniLib:Resistor_Wide` fixture whose pins sit 5.08 mm out
instead of 2.54, so wire movement is actually exercised rather than asserted
against an identity transform. `MiniLib:ESP32_Demo`'s three pins were also
spread out; they had all been stacked on the symbol origin, which could not
test anything positional. That changed one count assertion in
`tests/test_phase2_indexer.py` from 3 symbols to 4.

---

## P2 — A placed symbol's fields are not autoplaced (Minor) — DONE

*From issue 7.*

`Reference` and `Value` text landed on the symbol origin, so `#PWR0001`
overlapped the GND graphic.

**Built.** `library_field_offsets` reads the library part's own field
placements, and `build_symbol_instance` applies them — rotated with the
instance, with the library's `justify` carried over. `add_symbol`,
`add_power_symbol` and `replace_symbol` all go through it.

The rotation direction was confirmed by measurement, not assumed: across KiCAD
10's demo schematics, 29 rotated symbols land exactly on the rotated library
offset under this transform and 0 under the opposite sign. It is the same
transform the pins use, which the phase 16 netlist gate independently validates.

**`(fields_autoplaced yes)` is deliberately not written.** The plan said to set
it "only when that is genuinely what was done", and it is not: KiCAD's
autoplace is its own algorithm over pin sides and bounding boxes. Of the
rotated demo symbols marked autoplaced, 29 sit on the rotated library offset
and 535 do not. Claiming the flag would assert something we did not do.
Library offsets are a large improvement on stacking every field at the origin;
"Autoplace Fields" in the GUI still does the real thing.

---

## P3 — Tool results do not report post-write validity (Minor) — DONE

*From issue 10.*

**Built.** A new `adapters/safe_write.py` carries the guarded write path, and
the three tool-level `_save_with_backup` helpers (schematic, pcb, rf) delegate
to it — so all 46 write sites are covered without touching any of them.

Stronger than the plan asked. Rather than returning `"load_ok": false` and
offering the backup, `save_tree` re-parses the file it just wrote, and on
failure **restores the backup and raises**, with the parse error and the backup
path in the message. A file that cannot be opened never survives the call that
made it, which is what issue 1 needed.

Re-parsing is cheap, so it is on by default. `KICAD_MCP_VERIFY_WRITES=0` turns
it off; an explicit `verify=` argument beats the environment. The `kicad-cli`
load check the plan mentioned is not wired in — it costs seconds per call, and
the re-parse catches the failure mode that actually occurred.

---

## P4 — `.backups/` grows without limit (Minor) — DONE

*From issue 9.*

**Built.** `safe_write.backup_file` prunes after each write, keeping the newest
`DEFAULT_KEEP_BACKUPS = 10` per file. `sch_editor.backup_file` delegates to it,
so every existing caller gets the limit. The timestamp gained microseconds, or
several writes inside one second would collide on a filename.

Ten is enough to undo a bad run by hand and few enough that a long session does
not bury the directory. The issue's 88-file session would now leave 10.

---

## P5 — No check that KiCAD has the project open (Minor) — DONE

*From the field report's closing notes.*

**Built.** `safe_write.check_not_locked` runs before every write and raises
`ProjectLockedError` when KiCAD's lock file exists. Both forms are checked: the
document's own `~<file>.lck` and the project's `~<project>.kicad_pro.lck`.

The error names the lock file it found and the override. `KICAD_MCP_IGNORE_LOCK=1`
exists for a lock left behind by a crash — not for editing a document KiCAD
really has open. The write is refused before the backup is taken, so a blocked
call leaves the file byte-identical.

---

## P6 — `(members ...)` wrapping does not match KiCAD (Minor) — DONE

*From `docs/issue8_formatting_probe.md`.*

First measured, then solved properly once KiCAD's source was to hand in
`source_repo/kicad`.

**The measurement was right that no line-length budget fits, and wrong about
why.** The rule is in `common/io/kicad/kicad_io_utils.cpp::Prettify`:

```cpp
const int consecutiveTokenWrapThreshold = 72;
...
if( inXY || column < consecutiveTokenWrapThreshold )
    formatted.push_back( ' ' );
else
    formatted += fmt::format( "\n{}", ... );   // wrap
```

The test is on the column *before* the next token, not on the finished line.
A line keeps taking tokens while the column is under 72, then ends wherever the
token that crossed the threshold happens to end — so observed widths run from
72 to 93 with no maximum that describes them. That is exactly what the probe
saw and could not explain.

The same function explains the other measured constant: `(xy ...)` lists pack
until `xySpecialCaseColumnLimit = 99`, and an `(xy ...)` token is about 19
characters, which is where the empirical `LINE_WIDTH = 118` came from.

**Built.** `adapters/kicad_prettify.py` is a line-for-line port of `Prettify`
(KiCAD 10, commit `9ffcbf13`). `sch_io.dumps` now serialises the tree flat and
runs it through the port — the same two steps KiCAD uses, `OUTPUTFORMATTER`
then `PRETTIFIED_FILE_OUTPUTFORMATTER`. The measured rules it replaces
(`LINE_WIDTH`, the `(pts ...)` packer, the `(data ...)` special case, the
over-long atom-list wrap) are all gone; one mechanism covers them.

`FORMAT_MODE.NORMAL` is the mode to use: `eeschema/schematic.cpp` picks
`COMPACT_TEXT_PROPERTIES` only when the `CompactSave` advanced setting is on,
and `advanced_config.cpp` defaults it to `false`.

**Result: 91 of 91** KiCAD 10-format demo schematics are byte-identical after
parse → dump, up from 66 of 78. The `(bus_alias ...)` deviation is gone, and so
is the class of problem — the layout now matches by construction rather than by
measured approximation.

### Boards are not byte-identical, for an unrelated reason

The same check over the 19 demo `.kicad_pcb` files gives 0 of 19, and every
difference is float formatting, not layout:

```
kicad: (dashed_line_dash_ratio 12.000000)
ours : (dashed_line_dash_ratio 12)
kicad: (xyz 0 -0 -0)
ours : (xyz 0 0 0)
```

KiCAD's board writer prints some fields with `%f` (six decimals) and preserves
negative zero. That is `sch_io._format_float`, not the prettifier, and it was
the same before this change — not a regression. Fixing it means knowing the
precision KiCAD uses per field, which is per-writer knowledge spread across
`pcbnew/`. Left alone; boards were never part of issue 8.

---

## P7 — `search_symbol` results are not brace-decoded (Minor) — DONE

*From the brace-escape work, 2026-09-18.*

**Built.** `search_symbol`, `search_footprint` and `get_symbol_details` now
decode KiCAD's `{brace}` escapes in the fields a caller reads — `description`,
`keywords`, `tags`, `datasheet`, `default_footprint`, and pin names.

`lib_id` and `name` are left exactly as the library spells them. They are
identity, not prose: callers hand them straight back to `get_symbol_details`
and `add_symbol`, and `lib_symbols_has` compares them against the tree.
Decoding them would break lookup, which is a worse bug than the display one.

---

## Not server bugs

Carried over from the field report's closing notes, kept here so they are not
re-investigated.

- **KiCAD library symbol.** `Regulator_Linear:TPS7A4701xRGW` omits NC pins 2
  and 17–19. Harmless, but check `get_symbol_details` pin lists against the
  datasheet before wiring.

---

## Closed since the field report

| Issue | Subject | Where it was fixed |
|---|---|---|
| 1 | Unescaped newlines made schematics unloadable | `sch_io._escape` escapes `\\`, `"`, `\n`, `\r`; the write is now re-parsed to prove it (P3) |
| 2 | Y-flip put every item off-grid | PCB moved to KiCAD-native Y-down; `pcb_to_file_xy` is the identity |
| 3 | `run_erc` could not find `kicad-cli` on Windows | `find_kicad_cli` honours `KICAD_CLI`, then PATH, then per-OS install paths |
| 4 | `list_components` ignored the active sheet | `scope="active"` / `"all"` |
| 5 | `#PWR` references duplicated across sheets | `all_references_in_hierarchy` |
| 6 | Missing edit primitives | Phase 3, plan points 3 and 5, and P1 |
| 7 | Symbols placed without autoplaced fields | P2 |
| 8 | Whole-file reformat on every write | KiCAD's `Prettify` ported (91/91 demo schematics byte-identical), line endings preserved (P6) |
| 9 | `.backups/` unbounded and untracked | `.gitignore`, then a retention limit (P4) |
| 10 | No post-write validity check | P3 |
