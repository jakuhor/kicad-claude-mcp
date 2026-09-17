"""Locate KiCAD library and binary paths across OS and KiCAD versions.

KiCAD exports env vars `KICAD{N}_SYMBOL_DIR` / `KICAD{N}_FOOTPRINT_DIR` only
inside its own GUI/scripting environment; outside (where this MCP server
runs), we fall back to platform defaults.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

# Versions to probe, newest first
_KICAD_VERSIONS = ("10", "9", "8", "7")


def _platform_default_symbol_dirs() -> list[Path]:
    """Default install locations for `.kicad_sym` libraries by OS."""
    sys_name = platform.system()
    if sys_name == "Darwin":
        return [
            Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"),
        ]
    if sys_name == "Linux":
        return [Path("/usr/share/kicad/symbols")]
    if sys_name == "Windows":
        return [
            Path(rf"C:\Program Files\KiCad\{v}\share\kicad\symbols")
            for v in _KICAD_VERSIONS
        ]
    return []


def _platform_default_footprint_dirs() -> list[Path]:
    sys_name = platform.system()
    if sys_name == "Darwin":
        return [
            Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"),
        ]
    if sys_name == "Linux":
        return [Path("/usr/share/kicad/footprints")]
    if sys_name == "Windows":
        return [
            Path(rf"C:\Program Files\KiCad\{v}\share\kicad\footprints")
            for v in _KICAD_VERSIONS
        ]
    return []


def _from_env(prefix: str) -> list[Path]:
    """Collect paths from `KICAD_LIBRARY_PATH` (custom) and KICAD{N}_{prefix}."""
    out: list[Path] = []
    custom = os.environ.get("KICAD_LIBRARY_PATH")
    if custom:
        out.extend(Path(p) for p in custom.split(os.pathsep) if p)
    for v in _KICAD_VERSIONS:
        env = os.environ.get(f"KICAD{v}_{prefix}")
        if env:
            out.append(Path(env))
    return out


def find_symbol_lib_dirs(extra: list[Path] | None = None) -> list[Path]:
    """Return existing directories that may contain `.kicad_sym` files.

    Search order: explicit `extra` arg → env vars → platform defaults.
    Filtered to existing directories. Duplicates removed (first wins).
    """
    candidates: list[Path] = []
    if extra:
        candidates.extend(extra)
    candidates.extend(_from_env("SYMBOL_DIR"))
    candidates.extend(_platform_default_symbol_dirs())
    return _dedup_existing(candidates)


def find_footprint_lib_dirs(extra: list[Path] | None = None) -> list[Path]:
    """Return existing directories that may contain `*.pretty/` libraries."""
    candidates: list[Path] = []
    if extra:
        candidates.extend(extra)
    candidates.extend(_from_env("FOOTPRINT_DIR"))
    candidates.extend(_platform_default_footprint_dirs())
    return _dedup_existing(candidates)


def _dedup_existing(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        rp = p.expanduser().resolve()
        if rp in seen or not rp.is_dir():
            continue
        seen.add(rp)
        out.append(rp)
    return out


def find_kicad_cli() -> Path | None:
    """Return path to `kicad-cli`, or None if unavailable.

    Order: `KICAD_CLI` env var, PATH, then per-OS install paths (newest KiCAD
    version first — several versions can coexist).
    """
    env = os.environ.get("KICAD_CLI")
    if env:
        p = Path(env)
        if p.is_file():
            return p
    found = shutil.which("kicad-cli")
    if found:
        return Path(found)
    for candidate in _platform_default_cli_paths():
        if candidate.is_file():
            return candidate
    return None


def _platform_default_cli_paths() -> list[Path]:
    """Default `kicad-cli` install locations by OS, newest version first."""
    sys_name = platform.system()
    if sys_name == "Darwin":
        return [Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")]
    if sys_name == "Linux":
        return [Path("/usr/bin/kicad-cli"), Path("/usr/local/bin/kicad-cli")]
    if sys_name == "Windows":
        roots = [
            Path(r)
            for r in (
                os.environ.get("ProgramFiles"),
                os.environ.get("ProgramFiles(x86)"),
                r"C:\Program Files",
            )
            if r
        ]
        out: list[Path] = []
        for root in roots:
            kicad = root / "KiCad"
            # Install dirs are named "10.0", "7.0", … — newest first.
            for version_dir in sorted(
                (d for d in _safe_iterdir(kicad) if d.is_dir()),
                key=lambda d: _version_key(d.name),
                reverse=True,
            ):
                p = version_dir / "bin" / "kicad-cli.exe"
                if p not in out:
                    out.append(p)
            # Fall back to the bare version names when nothing was listed.
            for v in _KICAD_VERSIONS:
                p = kicad / v / "bin" / "kicad-cli.exe"
                if p not in out:
                    out.append(p)
        return out
    return []


def _safe_iterdir(p: Path) -> list[Path]:
    try:
        return list(p.iterdir())
    except OSError:
        return []


def _version_key(name: str) -> tuple:
    """Sort key for a version directory name like "10.0"; unparsable names sort last."""
    parts = []
    for chunk in name.split("."):
        if not chunk.isdigit():
            return (-1,)
        parts.append(int(chunk))
    return tuple(parts) if parts else (-1,)


def cache_dir() -> Path:
    """Per-user cache directory for the indexer."""
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    p = base / "kicad-claude"
    p.mkdir(parents=True, exist_ok=True)
    return p
