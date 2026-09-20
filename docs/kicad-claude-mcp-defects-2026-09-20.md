# kicad-claude-mcp — defects found during base-board layout

**Reported:** 2026-09-20
**Server:** Greg3001/kicad-claude-mcp, served from `C:/Users/Jakub/github/kicad-claude-mcp`
**KiCad:** 10.0.6 (`C:\Program Files\KiCad\10.0`)
**Project used:** `firbox/hardware/base_board` — 189 footprints, 90 nets, 4 copper layers

Found while taking a fully annotated schematic through to a placed, planed
board. Ordered by severity. Each item has the reproduction and the workaround
that was actually used.

---

## 1. `move_footprint(rotation=...)` rotates pad positions but not the pads

**Severity: high — produces silently wrong copper.**

Rotating a footprint updates the footprint's own `(at x y rot)`. Each pad keeps
its own angle at `0`. KiCad treats a pad's angle as absolute, so the pad
*positions* rotate with the footprint while the pad *bodies* stay in their
original orientation.

Reproduction:

```
move_footprint(reference="J1", x_mm=16, y_mm=60, rotation=90)
```

with `J1 = Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal`.
Its lands are 0.6 mm × 1.15 mm on a 0.8 mm row pitch. After the rotation the
positions form a vertical row, but every pad is still 0.6 mm wide by 1.15 mm
tall, so adjacent pads overlap by 0.35 mm.

Verification through KiCad's own Python:

```python
f = b.FindFootprintByReference("J1")
f.GetOrientationDegrees()          # 90.0
[p.GetOrientationDegrees() for p in f.Pads()]   # all 0.0
# A1 bbox y 62.625..63.775, A4 bbox y 61.825..62.975  -> 0.35 mm overlap
```

DRC reported 29 `shorting_items` and 25 `solder_mask_bridge` violations, all
inside J1, for example:

```
Items shorting two nets (nets /VBUS and GND_USB)
Front solder mask aperture bridges items with different nets
```

This reads as a broken footprint, not as a tool bug, which is what makes it
expensive — the footprint is a stock KiCad one and is fine.

Expected: rotating a footprint should set each pad's angle to
`(pad_local_angle + footprint_rotation) % 360`, as KiCad itself does when a
footprint is rotated in the GUI.

Workaround used: after `move_footprint`, walk the footprint's pads and append
the footprint angle to each pad's `(at …)`. Afterwards `GetOrientationDegrees()`
returns 90.0 for every pad, the lands are 1.15 mm × 0.6 mm, A1 to A4 clears by
0.2 mm, and all 54 violations disappear.

---

## 2. Unknown keyword arguments are accepted and silently ignored

**Severity: high — the call looks like it succeeded and the design is unchanged.**

`set_design_rules` takes `min_clearance_mm`, `min_track_width_mm`, and so on.
Calling it with the un-suffixed names returns HTTP success and a rules dict —
the *old* rules, unchanged, with no error and no warning:

```
set_design_rules(min_clearance=0.15, min_track_width=0.127, min_via_drill=0.2)
-> {"rules": {..., "min_clearance": 0.127, "min_via_drill": 0.254, ...}}
```

Same shape of problem on `add_net_class`, which takes `clearance_mm` /
`track_width_mm`:

```
add_net_class(name="Default", clearance=0.15, track_width=0.25)
-> {"net_class": {..., "clearance": 0.2, "track_width": 0.2, ...}}
```

The returned object is the current state, so it is easy to read the response as
confirmation. The only clue is that the numbers did not move.

Expected: reject unknown keyword arguments with an error naming the accepted
parameters, or accept both spellings.

---

## 3. Footprints in the KiCad 6 format import with reference `?` and lose their nets

**Severity: high — silent partial import.**

`mylib.pretty` held a mix of formats. Footprints written with the old
`(fp_text reference "REF**" …)` form — as opposed to `(property "Reference" …)`
— are placed on the board, but `update_pcb_from_schematic` then cannot read
their reference. It reports them as still missing, reports the just-placed
copies as orphans, and assigns none of their pads:

```
"missing_in_pcb": ["L9", "U2"],
"orphan_footprints": ["REF**", "REF**"],
"pcb_references": 188,   "schematic_references": 189
```

Each subsequent run places another unnamed copy. The nets on those parts —
`GND_USB`, `GND`, `VBUS_raw_+20V`, `VBUS_cm_+20V` on the L9 common-mode choke —
are simply absent, which on this board means the one connection between the two
ground domains is missing.

Expected: read `(fp_text reference …)` as a fallback when no
`(property "Reference" …)` is present, on both the read and the write side; or
at minimum fail loudly instead of placing an unnamed footprint.

Workaround used: `kicad-cli fp upgrade <lib>.pretty` on the library, then
re-run the sync with `remove_extra=True` to clear the `REF**` copies.

---

## 4. Pads are written as `(net "NAME")` with no index and no board net table

**Severity: medium — non-canonical file format.**

Pads written by the server carry the net name only:

```
(pad "1" smd roundrect
    (at -0.51 0)
    (size 0.54 0.64)
    (layers "F.Cu" "F.Mask" "F.Paste")
    (net "GND_USB")
    (uuid "7220d9aa-0e35-4a59-913f-c9f50f822873")
)
```

The board-level net table holds only `(net 0 "")` — none of the 90 nets is
declared, and no pad references a net index. KiCad 10.0.6 does load this:
`pcbnew.LoadBoard(...).GetNetsByName()` returns 91 entries and DRC resolves
names correctly. It is still not the format KiCad writes, so the tolerance is
not something to rely on across versions, and any third-party parser expecting
`(net <index> "<name>")` on pads plus a net table will read the board as having
no nets at all.

Expected: emit the numbered net table and numeric pad net references, the same
as pcbnew.

---

## 5. `update_pcb_from_schematic` exceeds its own default timeout on a mid-size board

**Severity: medium.**

On 189 footprints / 90 nets the call reliably exceeds the default
`timeout_seconds=90` and returns:

```
Error executing tool update_pcb_from_schematic: pcbnew script timed out after 90.0s
```

The board is left partially updated — footprints placed, nets not assigned
(`footprints: 183, nets: 0`) — and re-running repeats the same partial work and
times out again, so without raising the timeout there is no way to converge.
Raising it to 550 s works; the call then takes roughly two to four minutes.

Two things would help:

- Raise the default, or scale it with the component count.
- On timeout, say what was completed. The current message gives no indication
  that footprints were placed but the netlist stage never ran.

---

## 6. `index_libraries` ignores libraries registered in the global `fp-lib-table`

**Severity: medium.**

`mylib` is registered globally:

```
(lib (name "mylib")(type "KiCad")(uri "${KICAD_MY}/mylib.pretty")(options "")(descr ""))
```

with `KICAD_MY = C:\Users\Jakub\github\kicad-library` in `kicad_common.json`.
Neither `index_libraries()` nor `index_libraries(force=True)` picks it up — the
indexer only walks the KiCad install directories — so every lookup fails with:

```
"unknown footprint lib_id 'mylib:IC_DMP3028LSD-13_DIO' — call index_libraries first or check spelling"
```

even though `index_libraries` had just been called, which makes the hint
misleading.

Expected: read the global `fp-lib-table` and `sym-lib-table` and expand the
KiCad path variables, as KiCad does.

Workaround used: add a project-local `fp-lib-table` next to the `.kicad_pro`
with the library's absolute path. Project-local tables *are* honoured, and the
lookup works immediately afterwards.

### 6b. `index_libraries` reports the wrong directory lists

Smaller bug, probably one line, in the same response. After `force=True`:

```json
"symbol_dirs":    ["C:\\Program Files\\KiCad\\10.0\\share\\kicad\\symbols",
                   "C:\\Program Files\\KiCad\\10.0\\share\\kicad\\footprints"],
"footprint_dirs": ["C:\\Program Files\\KiCad\\10.0\\share\\kicad\\symbols",
                   "C:\\Program Files\\KiCad\\10.0\\share\\kicad\\footprints"]
```

Both keys get the concatenation of both directory lists. The cached (non-forced)
response reports them correctly, one directory each.

---

## 7. `run_drc` has no summary mode

**Severity: low — usability, but it costs a lot in an agent context.**

Every `run_drc` call on this board returns 200–360 kB of JSON (8 000–14 000
lines), which is past the tool-result limit and has to be written to a file and
post-processed just to learn how many violations there are and of what type.
`severity="error"` does not help much, because unrouted ratsnest lines dominate
until the board is routed.

A `summary=True` mode returning counts by `type` and `severity`, or a
`max_violations` cap, would make the tool usable directly. The same applies to
`list_unrouted`, which returned 129 kB to communicate `count: 358`.

---

## 8. `apply_fab_preset` has no 4-layer JLCPCB preset

**Severity: low — missing data, not a bug.**

Available presets are `jlcpcb_2l_advanced`, `jlcpcb_2l_default`, `oshpark_4l`,
`pcbway_default`, `permissive_prototype`. For a 4-layer board the only option
is `oshpark_4l`, whose 0.254 mm minimum drill and 0.33 mm minimum through-hole
reject the 0.2 mm thermal vias inside stock KiCad footprints such as
`Package_DFN_QFN:Texas_RGW0020A_VQFN-20-1EP_5x5mm_P0.65mm_EP3.15x3.15mm_ThermalVias`
— 18 `drill_out_of_range` errors on two ICs, none of which is a real
manufacturing problem at JLCPCB.

A `jlcpcb_4l` preset (0.2 mm via drill, 0.127 mm clearance and track) would
cover the common case.

---

## Note on netclass clearance, not a defect

Worth documenting somewhere in the tool descriptions, because it cost a
diagnosis cycle: a netclass `clearance` also applies pad-to-pad *inside* a
single footprint. Setting a sensible 0.4 mm on power classes produced 119
clearance errors against the 0.2–0.3 mm land gaps of every QFN and SOT-583 on
the board. The rails' real spacing has to come from `add_drc_rule` with a
`A.Type == 'Track' && B.Type == 'Track'` condition instead. A sentence in the
`add_net_class` docstring would save the next person the round trip.

---

## Resolution — 2026-09-20

State of each item after the fixing round. Fast suite: 488 passed, 54 deselected (slow / network).

**1. Rotated footprints shear their pads — fixed.**
`pcb_editor.rotate_footprint_pads` adds the rotation delta to every pad's
`(at x y angle)`, because a pad's angle in a `.kicad_pcb` is absolute.
`move_footprint` applies the delta `new_rotation - old_rotation`, and
`add_footprint` applies the placement rotation to the library pads. Acceptance:
`test_acceptance_rotating_a_real_footprint_keeps_drc_clean` places the reported
GCT USB-C and asserts the DRC error count at 90° equals the count at 0° (74 → 12
errors with the fix; 12 is what the bare footprint produces against the blank
project's default rules at either angle).

**2. Unknown keyword arguments — fixed for every tool.**
`kicad_claude/strict_args.py` wraps the FastMCP tool manager once and raises a
`ToolError` naming the unknown argument, the closest accepted spelling and the
full accepted list. FastMCP's own argument model ignores extras, so this had to
sit above it rather than in each signature.

**3. KiCad 6 footprints lose their reference — fixed.**
`get_footprint_reference` falls back to `(fp_text reference …)`, `add_footprint`
writes the reference and value into the legacy nodes as well, and
`set_footprint_property` updates an existing `fp_text` instead of adding a
second, conflicting field. `kicad-cli pcb drc` loads such a board with 0 errors
and keeps the reference.

**4. Pads written as `(net "NAME")` — not a defect; no change.**
This *is* the KiCad 10 format. Upgrading a stock demo board with
`kicad-cli pcb upgrade` (format `20250513` → `20260206`) rewrites every
`(net <index> "<name>")` on a pad as `(net "<name>")` and drops the top-level
net table entirely — 0 `(net N "…")` lines remain. `pcbnew.SaveBoard` from
KiCAD 10.0.6 writes the same `(net "GND")` the server writes. The numbered form
belongs to board format 9 and earlier. The server keeps reading either.

Made discoverable rather than changed, so the next reader does not re-derive it:
the `list_nets` docstring states which spelling KiCAD 10 uses, and
`get_project_state` now returns `formats` — `sch_version`, `pcb_version` and
`pcb_net_storage` — so the format is in a tool response, not only in prose.

One real defect surfaced while documenting this: the blank template carried a
stale `(net 0 "")`, which `has_net_table` counted as a legacy table, so every
zone and track written on a fresh project took the index branch and emitted the
pre-KiCAD-10 `(net 0) (net_name "GND")` spelling. `has_net_table` now ignores
the unconnected-net placeholder and the template line is gone — KiCAD drops it
on its own first save. A fresh board now gets `(net "GND")` on both zone and
track, DRC 0 errors.

**5. `update_pcb_from_schematic` timeout — fixed.**
`timeout_seconds` now defaults to 0, meaning "scale with the design":
`max(180, 120 + 2.5 × components)`, so the 189-part board gets ~590 s. A timeout
in the netlist stage no longer only raises: the response carries `timed_out`,
`stage`, `completed` ("footprints placed and saved; pad nets NOT assigned") and a
hint with a larger value to retry with.

**6. Global `fp-lib-table` ignored — fixed.**
New `utils/kicad_config.py` reads KiCAD's own config directory
(`%APPDATA%/kicad/<version>`), expands `${VAR}` from `kicad_common.json`'s
`environment.vars`, the environment, and the installed share directories, and
hands the library directories to the indexer. `mylib` from the report now
indexes (15 841 footprints, 23 209 symbols here). Project-local tables get the
same variable expansion, so a `${KICAD_MY}` uri works there too.

**6b. Both directory lists got both directories — fixed.**
The cause was `KICAD_LIBRARY_PATH`: one list feeding both searches. Candidate
directories are now filtered by what they actually hold — `*.kicad_sym` for
symbol directories, `*.pretty` for footprint ones.

**7. No summary mode — added.**
`run_drc(summary=True)` returns `violations_by_type`,
`unconnected_items_by_type` and `schematic_parity_by_type` (one row per type and
severity) with the entry lists emptied and `<list>_omitted` counts;
`max_violations=N` caps each list instead. `list_unrouted` gained `summary`,
`max_items` and a `net` filter, plus a `by_net` roll-up. Defaults are unchanged.

**8. No 4-layer JLCPCB preset — added.**
`jlcpcb_4l`: 0.127 mm clearance and track, 0.2 mm via drill, 0.2 mm minimum
through-hole, which accepts the 0.2 mm thermal vias in stock KiCad QFN
footprints.

**Netclass clearance note — documented.**
The `add_net_class` docstring now says that `clearance_mm` also applies
pad-to-pad inside a single footprint, and points at `add_drc_rule` with an
`A.Type == 'Track' && B.Type == 'Track'` condition for rail spacing.
