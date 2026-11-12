"""Operating-temperature ranges and grades for the lifecycle audit.

A part's range comes from its extracted datasheet (`recommended.T_A`, else
`recommended.T_J`) or, failing that, the distributor's operating-temperature
field. Grades follow kicad-happy 3.0.1 (MIT, © 2025 Andrew Klofas):
kicad/scripts/lifecycle_engine.py `_classify_temp_grade`.
"""

from __future__ import annotations

import re

TA_KEYS = ("T_A", "TA", "T_amb", "T_OP", "TOPR", "T_opr")
TJ_KEYS = ("T_J", "TJ")
_NUM = r"([-+−–]?\s*\d+(?:\.\d+)?)"


def classify(lo: float, hi: float) -> str:
    """military / automotive / extended / industrial / commercial / non-standard."""
    if lo <= -55 and hi >= 125:
        return "military"
    if lo <= -40 and hi >= 125:
        return "automotive"
    if lo <= -40 and hi >= 105:
        return "extended"
    if lo <= -40 and hi >= 85:
        return "industrial"
    if lo <= 0 and hi >= 70:
        return "commercial"
    return "non-standard"


def _num(t: str) -> float:
    return float(re.sub(r"\s", "", t).replace("−", "-").replace("–", "-"))


def parse_range(text: str) -> tuple[float, float] | None:
    """(min, max) °C from a distributor field: "-40°C ~ 85°C (TA)",
    "-55 C to +125 C", "-40 ... 105 °C"; None when no two numbers."""
    if not text:
        return None
    m = re.search(_NUM + r"\s*°?\s*C?\s*(?:~|to|\.\.\.?|…|/|,)\s*" + _NUM, text, re.I)
    if not m:
        return None
    a, b = _num(m.group(1)), _num(m.group(2))
    return (min(a, b), max(a, b))


def from_specs(rec: dict | None) -> tuple[float, float, str] | None:
    """(min, max, source) from the record's recommended ambient (else
    junction) temperature; None without both ends."""
    for keys, what in ((TA_KEYS, "T_A"), (TJ_KEYS, "T_J")):
        for k in keys:
            sv = ((rec or {}).get("recommended") or {}).get(k)
            if sv and sv.get("min") is not None and sv.get("max") is not None:
                return (float(sv["min"]), float(sv["max"]),
                        f"datasheet {rec.get('mpn')} p.{sv.get('page')} ({what})")
    return None


def from_sources(hits: dict) -> tuple[float, float, str] | None:
    """(min, max, source) from the first source with a parsable range."""
    for name, h in hits.items():
        rng = parse_range(h.get("operating_temp") or "")
        if rng:
            return rng[0], rng[1], f"{name} operating temperature"
    return None


def attr_text(attrs: list, name_key: str = "attributeLabel",
              value_key: str = "attributeValue") -> str:
    """Operating-temperature text from a distributor attribute list: one
    "Operating Temperature" value, or "min ~ max" from separate Minimum /
    Maximum entries; "" when absent."""
    lo = hi = whole = ""
    for a in attrs:
        if not isinstance(a, dict):
            continue
        name = str(a.get(name_key) or "").lower()
        val = f"{a.get(value_key) or ''} {a.get('attributeUnit') or ''}".strip()
        if "operating temperature" not in name or not val:
            continue
        if "min" in name:
            lo = val
        elif "max" in name:
            hi = val
        else:
            whole = val
    return whole or (f"{lo} ~ {hi}" if lo and hi else "")
