# kicad-claude-mcp

## Project overview
MCP server that reads and modifies KiCad files. It must never corrupt a file,
and it must preserve KiCad's own formatting so that a project's git diff stays
minimal.

Focus: design work, design for manufacturing, and design review through the
`kicad-happy` plugin. Manufacturing output, panelization and automated ordering
exist in the code but are **not** a current focus — do not extend them unless
asked.

## Do NOT
- **Do not create commits.** Never run `git commit`, `git add` + commit, or any
  command that creates a commit, under any circumstances — regardless of how
  complete or "safe" a change looks. Leave the working tree dirty for manual
  review and commit.
- Do not push, tag, or open PRs.

## Current backlog
`kicad_mcp_issues.md` (untracked) is the live bug queue — a field report from
real use on the Firbox base board. Work it top-down: issue 1 (unescaped control
characters in `adapters/sch_io.py::_escape`, which makes schematics unloadable)
is a blocker.

## Layout
- `server.py` — entry point; creates `FastMCP("kicad-claude")` and calls each
  `tools/<group>.register(mcp)`. 130 tools total.
- `src/kicad_claude/tools/*.py` — thin MCP tool wrappers: validate arguments,
  call an adapter, return a JSON-friendly dict.
- `src/kicad_claude/adapters/*.py` — the real work: s-expression IO
  (`sch_io.py`), editors (`sch_editor.py`, `pcb_editor.py`), `kicad_cli.py`,
  vendor APIs, calculations.
- `src/kicad_claude/indexer/` — library scan and fuzzy search.
- `src/kicad_claude/utils/` — `geometry.py` (mm and Y-axis conversions),
  `kicad_paths.py`, `logging.py` (stderr only; stdout belongs to MCP).
- `src/kicad_claude/state.py` — `ActiveProject` singleton; one active project
  per server process.
- `tests/test_phase<N>_*.py` — one file per feature phase.

## File-format rules
- Round-trip safety first: parse, mutate the tree, write back. A write must
  touch only the nodes it changed — no whole-file reformat, no re-ordering, no
  re-indent of untouched blocks.
- Escape every control character on write (backslash, `"`, `\n`, `\r`, `\t`).
  `sexpdata` decodes escapes on read, so a naive dump corrupts the file.
- Preserve existing UUIDs. Generate a new UUID only for a new item.
- Keep the format versions KiCad 10 writes: `.kicad_sch` `20250114`,
  `.kicad_pcb` `20241229`, `.kicad_pro` `meta.version` 3.
- Mutating tools back up to `<project>/.backups/<timestamp>_<file>` before
  writing.
- Prove a write is still valid with `kicad-cli` (`sch erc`, `pcb drc`), not by
  eye.

## Commands
```bash
uv sync
uv run pytest -m "not slow and not network" -q   # fast tests (~3 s)
uv run pytest -m "slow" -q                       # acceptance, needs kicad-cli
uv run pytest -m "network" -q                    # live DigiKey/Mouser
uv run ruff check .
uv run mcp dev server.py                         # MCP Inspector
```
Add a test to the matching `tests/test_phase<N>_*.py` for every new or fixed
tool.

## KiCad
- **KiCad 10.0.6** at `C:\Program Files\KiCad\10.0` — the version to use.
- `kicad-cli` at `C:\Program Files\KiCad\10.0\bin\kicad-cli.exe`.

## Docs
English for all documentation and code comments.

## Response style
The `caveman` plugin is installed. Enable it with `/caveman:caveman`; it is the
default style for this project.

## Working style (Karpathy's 4)
1. **Think before doing.** State assumptions about paths, versions and file
   formats before writing. Two plausible readings → present both.
2. **Simplicity first.** The minimum change that works. No speculative
   abstraction over `sexpdata` or `kicad-cli`.
3. **Surgical changes.** Every changed line traces to the request. Notice
   something unrelated → mention it, leave it.
4. **Verifiable goals.** Finish with the command that proves it. Unverifiable
   from here → say so and hand the user the check.
