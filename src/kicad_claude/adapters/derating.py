"""Derating profiles: how far below its rating a part may run.

These are design-policy values, not part data. The capacitor and IC
fractions of `commercial`, `automotive` and `military` follow kicad-happy
3.0.1's profiles (kicad/scripts/schematic/system_analyses.py
`_DERATING_PROFILES`, MIT, © 2025 Andrew Klofas); `industrial` sits between
commercial and automotive, and the junction-temperature margins are this
project's policy (commercial keeps the former 25 °C default).

  cap      fraction of the rated voltage a capacitor may see, per dielectric
           family (ceramic, electrolytic, tantalum; unknown uses `unknown`);
  ic_abs   fraction of an IC pin's absolute-maximum voltage (CR-PIN-DERATE);
  r_power  fraction of a resistor's power rating (RT-R-P);
  tj_margin_c  required distance of Tj below Tj max (RT-TJ warning).
"""

from __future__ import annotations

PROFILE_NAMES = ("commercial", "industrial", "automotive", "military")

PROFILES: dict[str, dict] = {
    "commercial": {"cap": {"ceramic": 0.50, "electrolytic": 0.80, "tantalum": 0.80,
                           "unknown": 0.50},
                   "ic_abs": 0.90, "r_power": 0.50, "tj_margin_c": 25.0},
    "industrial": {"cap": {"ceramic": 0.50, "electrolytic": 0.75, "tantalum": 0.70,
                           "unknown": 0.50},
                   "ic_abs": 0.85, "r_power": 0.50, "tj_margin_c": 25.0},
    "automotive": {"cap": {"ceramic": 0.50, "electrolytic": 0.70, "tantalum": 0.60,
                           "unknown": 0.50},
                   "ic_abs": 0.85, "r_power": 0.50, "tj_margin_c": 30.0},
    "military": {"cap": {"ceramic": 0.40, "electrolytic": 0.60, "tantalum": 0.50,
                         "unknown": 0.40},
                 "ic_abs": 0.80, "r_power": 0.50, "tj_margin_c": 40.0},
}

_CERAMIC = ("X5R", "X7R", "X6S", "X7S", "X8R", "C0G", "NP0", "COG", "Y5V", "Z5U", "MLCC",
            "CERAMIC")


def profile(name: str) -> dict:
    if name not in PROFILES:
        raise ValueError(f"derating_profile {name!r} not in {list(PROFILE_NAMES)}")
    return PROFILES[name]


def cap_family(comp: dict, rec: dict | None = None) -> str:
    """ceramic / electrolytic / tantalum / unknown from the symbol (value,
    footprint, lib_id, fields, description) and the spec record's
    description."""
    text = " ".join([comp.get("value", ""), comp.get("footprint", ""), comp.get("lib_id", ""),
                     comp.get("description", ""), " ".join((comp.get("fields") or {}).values()),
                     (rec or {}).get("description", "")]).upper()
    if "TANTAL" in text:
        return "tantalum"
    if any(k in text for k in _CERAMIC):
        return "ceramic"
    if any(k in text for k in ("ELECTROLYTIC", "ELCO", "CP_ELEC", "POLYMER", "C_POLARIZED",
                               "CP_RADIAL", "CP_AXIAL")):
        return "electrolytic"
    if "CAPACITOR_SMD:C_" in text:
        return "ceramic"
    return "unknown"
