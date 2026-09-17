# Issue 8 — why an MCP write reformats the whole file

Investigation only; no writer change was made. Measured on 2026-09-17 against
the KiCad 10.0.6 demo schematics in
`C:\Program Files\KiCad\10.0\share\kicad\demos` (90 files, 78 of them in the
KiCad 10 format `(version 20250114)`).

Method: parse each KiCad-written file with `sch_io.parse_file`, dump it back
with `sch_io.dumps`, and diff against the original. Probe scripts are in the
session scratchpad (`fmt_probe*.py`) and are not part of the repo.

## Result

One rule accounts for almost the whole diff: **KiCad packs the children of
`(pts ...)` and `(members ...)` onto shared lines, wrapping at roughly 120
columns; our dumper writes one child per line.**

| Dumper | Byte-identical files (of 78) |
|---|---|
| current | 0 |
| plus `(xy ...)` / `(members ...)` packing at 120 columns | 34 |

Evidence for the wrap width: over 26 657 `(xy ...)` lines in the demos, the
longest is 118 characters and none exceeds 120 (a tab counted as one
character). Limits of 118–121 all give the same 34 files, so the exact value
needs one more experiment against a file KiCad itself rewrites.

## Remaining causes, in order of frequency

1. **`(members ...)` that fits on one line.** KiCad writes
   `(members "TX+" "TX-" "RX+" "RX-")` as a single line, head included, and only
   breaks when the list is too long. The packing prototype always broke after
   the head.
2. **`(color r g b a)` alpha.** KiCad prints the alpha with four decimals —
   `(color 255 255 255 1.0000)`, `(color 0 0 0 0.0000)`. `_format_float` trims
   to `1` and `0`. 51 lines across the KiCad 10 demos.
3. **Embedded files `(data "…")`.** Base64 arrives as many 76-character string
   atoms. KiCad puts the first chunk on the `(data` line and wraps the rest;
   we put every chunk on its own line. Affects every file with an embedded
   image (interf_u, csi, dcdc, expansion_connector, jetson-agx-thor-baseboard).
4. **Tab characters in strings.** KiCad writes a raw TAB inside a quoted string
   (e.g. `(property "Part Description" "<TAB>100 Position Connector …")`),
   while `_escape` now writes `\t`. Verified safe: a file we wrote with `\t`
   exports through `kicad-cli sch export netlist --format kicadxml` with the
   value read back as `'A\tB'`, so KiCad decodes the escape. It is a formatting
   difference, not a corruption. `\n` must stay escaped — KiCad escapes it too
   (`(text "CHANGE LOG\n\n- swapped sensors' I2C …")` in CM5_MINIMA_3).
5. **Indentation of pre-10 files.** Some demos are older and use two spaces per
   level instead of one tab. Not relevant while we only write `20250114`.

## What this implies for the fix

Matching KiCad byte for byte looks like four narrow rules in `sch_io.dumps`
(pack `pts`/`members`/`data`, single-line short lists, 4-decimal color alpha,
raw tab), not a rewrite. Worth doing before the surgical text-edit approach,
which is far larger.

A regression test should assert `parse → dump` is byte-identical for a set of
KiCad-written fixtures, run per format version.
