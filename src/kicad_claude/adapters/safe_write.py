"""The guarded write path every mutating tool goes through.

Three jobs, all of them about not losing work:

- **Refuse to write under KiCAD's nose.** KiCAD keeps a `~<file>.lck` beside an
  open document. Writing then races KiCAD's own save, and whichever writes last
  wins (P5).
- **Prove the file still parses.** A write that produces something unreadable is
  restored from the backup and raises, so a corrupt file never survives the call
  that made it. Issue 1 went unnoticed for a whole session for want of this (P3).
- **Keep the backup directory bounded.** One backup per write filled
  `<project>/.backups/` with 88 files in a single session (P4).
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from kicad_claude.adapters import sch_io

# Backups kept per file, newest first. Enough to undo a bad run by hand,
# few enough that a long session does not bury the directory.
DEFAULT_KEEP_BACKUPS = 10

# Set to 1 to write anyway when KiCAD holds the project open. For a lock left
# behind by a crash — not for editing a document KiCAD really has open.
IGNORE_LOCK_ENV = "KICAD_MCP_IGNORE_LOCK"

# Set to 0 to skip the post-write re-parse.
VERIFY_ENV = "KICAD_MCP_VERIFY_WRITES"


class ProjectLockedError(RuntimeError):
    """KiCAD has the project open."""


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no")


def lock_files_for(path: Path) -> list[Path]:
    """Lock files that would cover `path`: its own, and the project's."""
    path = Path(path)
    candidates = [path.parent / f"~{path.name}.lck"]
    for pro in path.parent.glob("*.kicad_pro"):
        candidates.append(path.parent / f"~{pro.name}.lck")
    return candidates


def check_not_locked(path: Path) -> None:
    """Raise `ProjectLockedError` when KiCAD holds `path` or its project open."""
    if _env_flag(IGNORE_LOCK_ENV, False):
        return
    for lock in lock_files_for(path):
        if lock.exists():
            raise ProjectLockedError(
                f"KiCAD has this project open — {lock} exists. Close the board "
                f"and schematic editors before editing the files, or KiCAD's "
                f"next save will overwrite these changes. If the lock is stale "
                f"after a crash, delete it or set {IGNORE_LOCK_ENV}=1."
            )


def backup_file(path: Path, keep: int = DEFAULT_KEEP_BACKUPS) -> Path | None:
    """Copy `path` to `<dir>/.backups/<timestamp>_<name>`, then prune old ones.

    Returns None when there is nothing to back up yet.
    """
    path = Path(path)
    if not path.is_file():
        return None
    backups = path.parent / ".backups"
    backups.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = backups / f"{stamp}_{path.name}"
    shutil.copy2(path, dest)
    prune_backups(backups, path.name, keep)
    return dest


def prune_backups(backups_dir: Path, filename: str, keep: int) -> int:
    """Keep the newest `keep` backups of `filename`. Returns how many went."""
    if keep <= 0:
        return 0
    existing = sorted(
        (p for p in backups_dir.glob(f"*_{filename}") if p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = 0
    for old in existing[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def save_tree(
    path: Path,
    tree: list,
    *,
    keep_backups: int = DEFAULT_KEEP_BACKUPS,
    verify: bool | None = None,
) -> Path | None:
    """Back up, write, and prove the result still parses. Returns the backup.

    On a verification failure the backup is put back and the exception carries
    both the parse error and the backup path, so the caller is never left with a
    file that cannot be opened.
    """
    path = Path(path)
    check_not_locked(path)

    backup = backup_file(path, keep=keep_backups)
    sch_io.write_file(path, tree)

    if verify is None:
        verify = _env_flag(VERIFY_ENV, True)
    if not verify:
        return backup

    try:
        sch_io.parse_file(path)
    except Exception as exc:
        if backup is not None:
            shutil.copy2(backup, path)
            raise RuntimeError(
                f"write to {path} produced a file that does not parse ({exc}); "
                f"restored from {backup}"
            ) from exc
        raise RuntimeError(
            f"write to {path} produced a file that does not parse ({exc}); "
            f"there was no previous version to restore"
        ) from exc
    return backup
