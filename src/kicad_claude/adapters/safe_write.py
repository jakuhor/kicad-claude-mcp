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


# KiCAD GUI executables that hold document locks (kicad-cli takes none).
_KICAD_GUI_PROCESSES = ("kicad", "eeschema", "pcbnew")


def kicad_gui_running() -> bool | None:
    """True if a KiCAD GUI process runs on this machine, None if unknown.

    KiCAD's lock file records only `username` and `hostname`, no pid
    (`include/lockfile.h`), so "is the owner still alive?" can only be answered
    as "is any KiCAD GUI running here?". Deliberately coarse: another open
    project also counts as running, which errs towards keeping the lock.
    """
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                timeout=10, check=False,
            ).stdout.lower()
            names = {line.split(",")[0].strip('"') for line in out.splitlines() if line}
            return any(f"{p}.exe" in names for p in _KICAD_GUI_PROCESSES)
        for p in _KICAD_GUI_PROCESSES:
            r = subprocess.run(["pgrep", "-x", p], capture_output=True, timeout=10,
                               check=False)
            if r.returncode == 0:
                return True
        return False
    except (OSError, subprocess.SubprocessError):
        return None


def lock_status(lock: Path) -> dict:
    """Who holds `lock`, and whether it is provably stale.

    `stale` is True only when the lock was written by this user on this host
    *and* no KiCAD GUI process is running here. An unreadable or empty lock
    file (older KiCAD, or written by hand) is never called stale.
    """
    import getpass
    import json
    import socket

    info: dict = {"path": str(lock), "username": None, "hostname": None}
    try:
        rec = json.loads(Path(lock).read_text(encoding="utf-8") or "null")
        if isinstance(rec, dict):
            info["username"] = rec.get("username")
            info["hostname"] = rec.get("hostname")
    except (OSError, ValueError):
        pass
    try:
        me = getpass.getuser()
    except Exception:  # noqa: BLE001 — getuser raises a variety of errors
        me = None
    host = socket.gethostname()
    info["same_user"] = bool(info["username"]) and (
        str(info["username"]).lower() == str(me).lower())
    info["same_host"] = bool(info["hostname"]) and (
        str(info["hostname"]).lower() == host.lower())
    running = kicad_gui_running()
    info["kicad_running"] = running
    info["stale"] = bool(info["same_user"] and info["same_host"] and running is False)
    return info


def check_not_locked(path: Path) -> None:
    """Raise `ProjectLockedError` when KiCAD holds `path` or its project open."""
    if _env_flag(IGNORE_LOCK_ENV, False):
        return
    for lock in lock_files_for(path):
        if lock.exists():
            st = lock_status(lock)
            if st["stale"]:
                diag = (f"The lock looks STALE: written by {st['username']} on "
                        f"{st['hostname']} (this machine) and no KiCAD process is "
                        f"running. Remove it with the clear_stale_lock tool.")
            elif st["kicad_running"]:
                diag = "A KiCAD process is running, so the lock is treated as live."
            else:
                diag = (f"Lock owner: {st['username'] or '?'}@{st['hostname'] or '?'} — "
                        "not provably this session's machine and user, so it is "
                        "treated as live.")
            raise ProjectLockedError(
                f"KiCAD has this project open — {lock} exists. Close the board "
                f"and schematic editors before editing the files, or KiCAD's "
                f"next save will overwrite these changes. {diag} To override "
                f"anyway set {IGNORE_LOCK_ENV}=1."
            )


def clear_stale_locks(path: Path) -> dict:
    """Delete the locks covering `path` that `lock_status` calls stale.

    Live or unprovable locks are reported, never touched.
    """
    removed, kept = [], []
    for lock in lock_files_for(path):
        if not lock.exists():
            continue
        st = lock_status(lock)
        if st["stale"]:
            lock.unlink()
            removed.append(st)
        else:
            kept.append(st)
    return {"removed": removed, "kept": kept}


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
