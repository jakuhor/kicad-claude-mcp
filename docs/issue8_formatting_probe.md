# Issue 8 — MCP writes no longer reformat the whole file

Measured against the KiCad 10.0.6 demo schematics in
`C:\Program Files\KiCad\10.0\share\kicad\demos` — 78 of them are in the KiCad 10
format `(version 20250114)`. Method: parse a KiCad-written file with
`sch_io.parse_file`, dump it back with `sch_io.dumps`, compare byte for byte.
Probe scripts live in the session scratchpad (`fmt_probe*.py`, `verify_fmt.py`)
and are not part of the repo.

## Result

| Dumper | Byte-identical files (of 78) |
|---|---|
| before | 0 |
| after | 66 |

One `add_wire` on the 3 519-line `tests/fixtures/kicad10_ecc83-pp_v2.kicad_sch`
now produces a 10-line diff — the wire itself. Before, a handful of edits gave
~6 000 changed lines.

## Rules implemented in `sch_io.dumps`

1. **`(pts ...)` packs its points.** The head stays alone on its line and the
   `(xy ...)` children are packed several to a line, wrapping at `LINE_WIDTH`.
   This was nearly the whole diff: our old dumper wrote one point per line.
2. **`LINE_WIDTH = 118`**, a tab counted as one column. 118 is the longest line
   KiCad 10 emits across 26 657 `(xy ...)` lines in its demos; 119 scores the
   same, 120 loses one file.
3. **An over-long atom list wraps** onto continuation lines indented one level,
   with the closing `)` on its own line — how KiCad writes `(members ...)`.
4. **`(data "…")` writes one base64 chunk per line**, including the short last
   chunk, with the first chunk on the head line.
5. **A sheet's background alpha gets four decimals**:
   `(sheet … (fill (color 0 0 0 0.0000)))`. Elsewhere the alpha is plain (`0`).
6. **Floats keep full precision** (`repr`), so a value such as
   `59.209102362204725` survives instead of being truncated to six decimals.
7. **A TAB inside a string stays raw**, which is what KiCad writes (see the
   "Part Description" properties in its CM5 demo). Verified that the escape is
   decoded either way: a file we wrote with `\t` came back as `'A\tB'` through
   `kicad-cli sch export netlist --format kicadxml`. `\n` and `\r` stay
   escaped — KiCad escapes those too, and a raw newline inside a quoted string
   makes KiCad refuse to load the file (issue 1).

## Known remaining deviation

The 12 files that still differ all differ only in **`(members ...)` wrapping**
inside `(bus_alias ...)`. KiCad breaks those lines earlier than a 118-column
budget explains, and inconsistently between files: lines are kept at 92, 91 and
93 columns in `flash`, `vme_interface` and `ddr4-ps`, yet `peripherals` wraps
where the next item would only reach column 78. No single column or item-count
rule fits all of them, so our writer wraps `members` at `LINE_WIDTH` like any
other long atom list. It affects bus aliases only, and KiCad loads the result.

## Regression tests

`tests/test_phase3_schematic.py`:

- `test_dump_of_kicad_written_file_is_byte_identical` and
  `test_write_file_of_kicad_written_file_changes_nothing` — parse → dump of
  `tests/fixtures/kicad10_ecc83-pp_v2.kicad_sch` (a KiCad 10 demo sheet)
  reproduces it exactly.
- `test_pts_points_are_packed_and_wrapped`, `test_data_chunks_are_one_per_line`,
  `test_sheet_fill_alpha_has_four_decimals`, `test_float_keeps_full_precision`
  cover the individual rules.

Re-run the corpus measurement after any change to `dumps`; the fixture test
alone does not cover embedded images or sheet fills.
