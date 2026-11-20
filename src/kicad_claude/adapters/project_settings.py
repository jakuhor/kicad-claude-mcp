"""Read and write `.kicad_pro` settings — design rules, net classes, patterns.

The `.kicad_pro` file is JSON. Two areas matter for engineering work:

    board.design_settings.rules     — DRC numerical minimums
    net_settings.classes            — net classes (track widths per group)
    net_settings.netclass_patterns  — assignments of nets to classes

KiCAD reformats this file freely on save; we just keep keys we care about
and leave everything else untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #


def load_pro(pro_path: Path) -> dict:
    return json.loads(Path(pro_path).read_text(encoding="utf-8"))


def save_pro(pro_path: Path, data: dict) -> Path | None:
    """Write `.kicad_pro` through the guarded writer (lock check, backup,
    re-parse). Returns the backup."""
    from kicad_claude.adapters import safe_write

    return safe_write.save_text(
        Path(pro_path),
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        lambda p: json.loads(Path(p).read_text(encoding="utf-8")),
    )


# --------------------------------------------------------------------------- #
# Design rules
# --------------------------------------------------------------------------- #


def get_design_rules(pro: dict) -> dict:
    return (
        pro.setdefault("board", {})
        .setdefault("design_settings", {})
        .setdefault("rules", {})
    )


def update_design_rules(pro: dict, **rules: Any) -> dict:
    """Merge non-None rules into the design_settings.rules dict."""
    rules_dict = get_design_rules(pro)
    for k, v in rules.items():
        if v is None:
            continue
        rules_dict[k] = v
    return rules_dict


# Fab presets: `data/fab_presets.json`, one entry per fab process —
# `description`, optional `source` (the fab's capability page), and `rules`
# (`.kicad_pro` design_settings.rules keys, values in mm). Add a fab there.
FAB_PRESETS_PATH = Path(__file__).resolve().parent.parent / "data" / "fab_presets.json"
FAB_PRESETS: dict[str, dict] = json.loads(FAB_PRESETS_PATH.read_text(encoding="utf-8"))


PRESET_MODES = ("project", "preset", "stricter")


def _stricter(rule: str, a: Any, b: Any) -> Any:
    """The tighter of two values of one rule.

    `min_*` rules: the larger minimum. `allow_*` flags: False (disallowed) wins.
    Anything else: keep `a` (the project's value).
    """
    if rule.startswith("allow_") and isinstance(a, bool) and isinstance(b, bool):
        return a and b
    if rule.startswith("min_") and isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return max(a, b)
    return a


def compare_preset(current: dict, preset_rules: dict) -> list[dict]:
    """One row per preset rule: project value, preset value, stricter value.

    `conflict` is True when the project sets the rule to a different value.
    `preset_is` says which way the preset moves it: `looser`, `stricter`,
    `equal`, `unset_in_project`, or `different` (not orderable).
    """
    rows = []
    for rule, pval in preset_rules.items():
        if pval is None:
            continue
        if rule not in current:
            rows.append({"rule": rule, "project": None, "preset": pval,
                         "stricter": pval, "conflict": False,
                         "preset_is": "unset_in_project"})
            continue
        cval = current[rule]
        strict = _stricter(rule, cval, pval)
        if cval == pval:
            way = "equal"
        elif strict == cval and strict != pval:
            way = "looser"
        elif strict == pval and strict != cval:
            way = "stricter"
        else:
            way = "different"
        rows.append({"rule": rule, "project": cval, "preset": pval,
                     "stricter": strict, "conflict": cval != pval, "preset_is": way})
    return rows


def resolve_preset(rows: list[dict], mode: str) -> dict:
    """Rule values to write for a `compare_preset` table under `mode`.

    - `project`:  keep every value the project sets; fill only unset rules.
    - `preset`:   take the preset's value for every rule it defines.
    - `stricter`: per rule, the tighter of the two (see `_stricter`).
    """
    if mode not in PRESET_MODES:
        raise ValueError(f"mode must be one of {PRESET_MODES}, got {mode!r}")
    out = {}
    for r in rows:
        if r["project"] is None:
            out[r["rule"]] = r["preset"]
        elif mode == "preset":
            out[r["rule"]] = r["preset"]
        elif mode == "stricter":
            out[r["rule"]] = r["stricter"]
        else:
            out[r["rule"]] = r["project"]
    return out


# --------------------------------------------------------------------------- #
# Net classes
# --------------------------------------------------------------------------- #


# KiCAD's "Default" net class shape — we mirror it for new classes.
DEFAULT_NETCLASS = {
    "bus_width": 12,
    "clearance": 0.2,
    "diff_pair_gap": 0.25,
    "diff_pair_via_gap": 0.25,
    "diff_pair_width": 0.2,
    "line_style": 0,
    "microvia_diameter": 0.3,
    "microvia_drill": 0.1,
    "pcb_color": "rgba(0, 0, 0, 0.000)",
    "schematic_color": "rgba(0, 0, 0, 0.000)",
    "track_width": 0.25,
    "via_diameter": 0.6,
    "via_drill": 0.3,
    "wire_width": 6,
}


def get_net_classes(pro: dict) -> list:
    return pro.setdefault("net_settings", {}).setdefault("classes", [])


def find_net_class(pro: dict, name: str) -> dict | None:
    for c in get_net_classes(pro):
        if c.get("name") == name:
            return c
    return None


def add_or_update_net_class(
    pro: dict,
    name: str,
    *,
    track_width_mm: float | None = None,
    clearance_mm: float | None = None,
    via_diameter_mm: float | None = None,
    via_drill_mm: float | None = None,
    diff_pair_width_mm: float | None = None,
    diff_pair_gap_mm: float | None = None,
    description: str = "",
) -> dict:
    """Add a new net class or update an existing one. Returns the class dict."""
    classes = get_net_classes(pro)
    existing = find_net_class(pro, name)
    if existing is None:
        cls: dict = {**DEFAULT_NETCLASS, "name": name}
        if description:
            cls["description"] = description
        classes.append(cls)
    else:
        cls = existing
        if description:
            cls["description"] = description

    if track_width_mm is not None:
        cls["track_width"] = float(track_width_mm)
    if clearance_mm is not None:
        cls["clearance"] = float(clearance_mm)
    if via_diameter_mm is not None:
        cls["via_diameter"] = float(via_diameter_mm)
    if via_drill_mm is not None:
        cls["via_drill"] = float(via_drill_mm)
    if diff_pair_width_mm is not None:
        cls["diff_pair_width"] = float(diff_pair_width_mm)
    if diff_pair_gap_mm is not None:
        cls["diff_pair_gap"] = float(diff_pair_gap_mm)
    return cls


def remove_net_class(pro: dict, name: str) -> bool:
    classes = get_net_classes(pro)
    for i, c in enumerate(classes):
        if c.get("name") == name:
            classes.pop(i)
            # Drop dangling pattern assignments
            patterns = pro.setdefault("net_settings", {}).setdefault(
                "netclass_patterns", []
            )
            patterns[:] = [p for p in patterns if p.get("netclass") != name]
            return True
    return False


def get_netclass_patterns(pro: dict) -> list:
    return pro.setdefault("net_settings", {}).setdefault("netclass_patterns", [])


def assign_pattern(pro: dict, *, netclass: str, pattern: str) -> dict:
    """Assign nets matching `pattern` (KiCAD glob) to `netclass`. Idempotent."""
    if find_net_class(pro, netclass) is None:
        raise KeyError(f"net class {netclass!r} doesn't exist; create it first")
    patterns = get_netclass_patterns(pro)
    entry = {"netclass": netclass, "pattern": pattern}
    for existing in patterns:
        if existing.get("netclass") == netclass and existing.get("pattern") == pattern:
            return existing
    patterns.append(entry)
    return entry
