# kicad-claude-mcp

## Project overview
MCP server that reads and modifies KiCad files. It must never corrupt a file,
and it must preserve KiCad's own formatting so that a project's git diff stays
minimal.

Focus: design work, design for manufacturing, and design review. The server is
meant to be the user's only tool: what the `kicad-happy` plugin does and this
server does not is being ported in (`docs/open_points.md`). Manufacturing
output, panelization and automated ordering exist in the code but are **not**
a current focus — do not extend them unless asked.

## Do NOT
- **Do not create commits.** Never run `git commit`, `git add` + commit, or any
  command that creates a commit, under any circumstances — regardless of how
  complete or "safe" a change looks. Leave the working tree dirty for manual
  review and commit.
- Do not push, tag, or open PRs.

## Open points and documentation
- `docs/open_points.md` is the only queue and holds **only open items**
  (OP-n, numbers never reused). New field reports and gaps go straight in as
  the next OP. Decisions taken with the user are marked *(decided, date)* —
  do not re-ask them.
- When an item is done: delete it from `open_points.md` and document the
  finished capability in `README.md` (Capabilities; the rule-ID table for new
  review rules). History lives in git, not in docs.
- Keep the counts in `README.md` (tools, tests) and below in step with the
  code.

## Layout
- `server.py` — entry point; creates `FastMCP("kicad-claude")`, registers
  `ping` and calls `toolsets.install(mcp)`. 142 tools total.
- `src/kicad_claude/toolsets.py` — the tool sets (`core`, `sch`, `pcb`, …).
  Set `<name>` is the module or package `tools/<name>` with `register(mcp)`;
  a package's `__init__` registers its submodules. Only `core` loads at
  startup; `load_toolset` adds sets at runtime. A new tool goes into the
  module of its set, and a set must stay inside its token budget
  (`tests/test_core_toolsets.py`).
- Fixed choices are `Literal` types, lists are `list[...]` — never a
  comma-joined string. Shared choice types live in `arg_types.py`.
- Tool docstrings: the text before a line holding only `Details:` is the
  listed description (≤ 500 chars: what, when, key arguments); everything
  after it is served by `help(tool)` only. Over budget → move text below
  `Details:`, do not raise the cap.
- `src/kicad_claude/tools/` — thin MCP tool wrappers: validate arguments,
  call an adapter, return a JSON-friendly dict. Helpers used by more than
  one set live in `tools/common.py`, not in another set's module.
- `src/kicad_claude/adapters/*.py` — the real work: s-expression IO
  (`sch_io.py`), editors (`sch_editor.py`, `pcb_editor.py`), `kicad_cli.py`,
  vendor APIs, calculations.
- `src/kicad_claude/indexer/` — library scan and fuzzy search.
- `src/kicad_claude/utils/` — `geometry.py` (mm and Y-axis conversions),
  `kicad_paths.py`, `logging.py` (stderr only; stdout belongs to MCP).
- `src/kicad_claude/state.py` — `ActiveProject` singleton; one active project
  per server process.
- `tests/test_<set>_<topic>.py` — tests filed by tool set (`test_sch_*`,
  `test_pcb_*`, `test_review_*`, …); `test_io_safe_write.py` for the writer.

## File-format rules
- Round-trip safety first: parse, mutate the tree, write back. A write must
  touch only the nodes it changed — no whole-file reformat, no re-ordering, no
  re-indent of untouched blocks. Layout comes from `adapters/kicad_prettify.py`,
  a port of KiCAD's own `Prettify`; all 91 KiCAD 10 demo schematics round-trip
  byte-identically. Do not hand-tune line widths there — fix the port against
  `source_repo/kicad` instead.
- Escape every control character on write (backslash, `"`, `\n`, `\r`, `\t`).
  `sexpdata` decodes escapes on read, so a naive dump corrupts the file.
- Preserve existing UUIDs. Generate a new UUID only for a new item.
- Keep the format versions KiCad 10.0.6 writes: `.kicad_sch` `20260306`,
  `.kicad_pcb` `20260206`, `.kicad_mod` `20260206`, `.kicad_sym` `20251024`,
  `.kicad_pro` `meta.version` 3. Read them back from the tool, not from
  `source_repo/kicad` (that checkout is master and runs ahead — board
  `20260901` as of 2026-09-17): `kicad-cli sch|pcb|fp|sym upgrade <file>`.
- Mutating tools write through `adapters/safe_write.save_tree`: it refuses to
  write while KiCAD holds a `.lck` on the project, backs up to
  `<project>/.backups/<timestamp>_<file>` keeping the newest 10, then re-parses
  the result and restores the backup if it does not parse.
- Prove a write is still valid with `kicad-cli` (`sch erc`, `pcb drc`), not by
  eye.

## Commands
```bash
uv sync
uv run pytest -m "not slow and not network" -q   # fast tests (~1 min)
uv run pytest -m "slow" -q                       # acceptance, needs kicad-cli
uv run pytest -m "network" -q                    # live DigiKey/Mouser
uv run ruff check .
uv run mcp dev server.py                         # MCP Inspector
```
Add a test to the matching `tests/test_<set>_*.py` for every new or fixed
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
