"""Read KiCAD's own user configuration: global library tables and path vars.

KiCAD keeps a global `sym-lib-table` / `fp-lib-table` next to
`kicad_common.json` in its configuration directory (on Windows
`%APPDATA%/kicad/<version>`). A library registered there — a personal
`mylib.pretty`, a company library — lives nowhere the platform defaults look,
so the indexer used to miss it entirely and every lookup failed with "unknown
footprint lib_id … call index_libraries first", which had already been called.

Library URIs use path variables (`${KICAD_MY}`, `${KICAD9_FOOTPRINT_DIR}`).
Those are resolved from `kicad_common.json`'s `environment.vars`, from the
process environment, and from the installed KiCAD share directories, exactly
as KiCAD resolves them.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
from pathlib import Path

from kicad_claude.utils.kicad_paths import (
    _platform_default_footprint_dirs,
    _platform_default_symbol_dirs,
    _safe_iterdir,
    _version_key,
)

logger = logging.getLogger("kicad-claude.kicad_config")

_VAR_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}|\$\(([A-Za-z0-9_]+)\)")
_VERSION_IN_PATH_RE = re.compile(r"[\\/](\d+)\.\d+[\\/]")


def _config_roots() -> list[Path]:
    """The per-user KiCAD configuration root, by OS."""
    sys_name = platform.system()
    if sys_name == "Windows":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "kicad"] if appdata else []
    if sys_name == "Darwin":
        return [Path.home() / "Library" / "Preferences" / "kicad"]
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return [Path(base) / "kicad"]


def config_dirs() -> list[Path]:
    """Versioned KiCAD config directories, newest version first."""
    out: list[Path] = []
    for root in _config_roots():
        versions = [d for d in _safe_iterdir(root) if d.is_dir()]
        out.extend(sorted(versions, key=lambda d: _version_key(d.name), reverse=True))
        if root.is_dir():
            out.append(root)  # KiCAD 5 and older kept the tables here
    return out


def _install_dir_vars() -> dict[str, str]:
    """`KICAD{N}_SYMBOL_DIR` / `KICAD{N}_FOOTPRINT_DIR` from the installs found."""
    out: dict[str, str] = {}
    for leaf, dirs in (
        ("SYMBOL_DIR", _platform_default_symbol_dirs()),
        ("FOOTPRINT_DIR", _platform_default_footprint_dirs()),
    ):
        for d in dirs:
            if not d.is_dir():
                continue
            m = _VERSION_IN_PATH_RE.search(str(d))
            if m:
                out.setdefault(f"KICAD{m.group(1)}_{leaf}", str(d))
            out.setdefault(f"KICAD_{leaf}", str(d))
    return out


def path_vars() -> dict[str, str]:
    """KiCAD's path substitutions: config vars, then env, then install defaults."""
    out: dict[str, str] = dict(_install_dir_vars())
    for cfg in reversed(config_dirs()):  # newest last, so it wins
        common = cfg / "kicad_common.json"
        if not common.is_file():
            continue
        try:
            data = json.loads(common.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("unreadable %s; ignoring its path variables", common)
            continue
        for name, value in (data.get("environment", {}).get("vars") or {}).items():
            if isinstance(value, str) and value:
                out[name] = value
    for name in list(out) + ["KICAD_MY"]:
        env = os.environ.get(name)
        if env:
            out[name] = env
    return out


def expand_uri(uri: str, variables: dict[str, str]) -> str | None:
    """Substitute `${VAR}` / `$(VAR)` in `uri`. None when a name is unknown."""
    missing: list[str] = []

    def _sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        value = variables.get(name)
        if value is None:
            missing.append(name)
            return ""
        return value

    expanded = _VAR_RE.sub(_sub, uri)
    if missing:
        return None
    return expanded


def global_lib_table_dirs(kind: str) -> list[Path]:
    """Directories named by KiCAD's global `sym-lib-table` / `fp-lib-table`.

    `kind` is "symbol" or "footprint". The indexer walks directories, so the
    parent of the `.kicad_sym` file / `.pretty` folder is returned. Only the
    newest config directory that has the table is read — that is the one KiCAD
    itself uses.
    """
    if kind == "symbol":
        filename, suffix = "sym-lib-table", ".kicad_sym"
    elif kind == "footprint":
        filename, suffix = "fp-lib-table", ".pretty"
    else:
        raise ValueError(f"kind must be 'symbol' or 'footprint' (got {kind!r})")

    table = next((c / filename for c in config_dirs() if (c / filename).is_file()), None)
    if table is None:
        return []

    variables = path_vars()
    out: list[Path] = []
    for uri in _table_uris(table):
        expanded = expand_uri(uri, variables)
        if expanded is None:
            continue  # a variable KiCAD knows and we do not; nothing to walk
        p = Path(expanded)
        if p.suffix.lower() != suffix:
            continue
        parent = p.parent
        if parent.is_dir() and parent not in out:
            out.append(parent)
    return out


def _table_uris(table_path: Path) -> list[str]:
    """Every `(uri "…")` in a lib table, read without a full s-expression parse.

    The tables are machine-written one lib per line; a regex keeps a malformed
    table from breaking indexing altogether.
    """
    try:
        text = table_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("failed to read %s: %s", table_path, e)
        return []
    return re.findall(r'\(uri\s+"((?:[^"\\]|\\.)*)"\s*\)', text)
