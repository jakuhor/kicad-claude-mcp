"""Package-typical junction-to-ambient thermal resistance (labelled estimate).

Used only when a part has no extracted datasheet `thermal.rth_ja`. Every
value is a JEDEC still-air figure for the package family, not for the part:
results built on it carry `rth_source: "package_typical (assumed)"` and a
finding resting on it is at most a warning. This is the project's only
table of values from general knowledge (decided 2026-11-12); an unknown
package gives no estimate (no generic fallback).
"""

from __future__ import annotations

import re

ASSUMED = "package_typical (assumed)"

# Ported from kicad-happy 3.0.1 (MIT, © 2025 Andrew Klofas):
# kicad/scripts/analyze_thermal.py:PACKAGE_THERMAL_RESISTANCE
# (footprint-name regex, Rθ_JA in °C/W); first match wins, specific first.
PACKAGE_RTH_JA: list[tuple[str, float]] = [
    (r"TO-263|D2PAK", 30.0),
    (r"TO-252|DPAK", 40.0),
    (r"TO-220", 25.0),
    (r"TO-92", 200.0),
    (r"SOT-223", 60.0),
    (r"SOT-89", 100.0),
    (r"SOT-23", 250.0),
    (r"SOT-363|SC-70", 300.0),
    (r"(?:QFN|DFN).*7[xX×]7", 20.0),
    (r"(?:QFN|DFN).*6[xX×]6", 22.0),
    (r"(?:QFN|DFN).*5[xX×]5", 25.0),
    (r"(?:QFN|DFN).*4[xX×]4", 35.0),
    (r"(?:QFN|DFN).*3[xX×]3", 50.0),
    (r"(?:QFN|DFN).*2[xX×]2", 70.0),
    (r"QFN|DFN", 40.0),
    (r"[TL]QFP.*144", 30.0),
    (r"[TL]QFP.*100", 35.0),
    (r"[TL]QFP.*(?:64|80)", 40.0),
    (r"[TL]QFP.*48", 50.0),
    (r"[TL]QFP.*32", 60.0),
    (r"[TL]QFP", 50.0),
    (r"(?:MSOP|VSSOP)(?!.*-\d*EP)", 200.0),
    (r"TSSOP[-_]?(?:20|24|28)(?![\d.x])(?!.*-\d*EP)", 80.0),
    (r"TSSOP[-_]?(?:14|16)(?![\d.x])(?!.*-\d*EP)", 100.0),
    (r"TSSOP[-_]?8(?![\d.x])(?!.*-\d*EP)", 150.0),
    (r"TSSOP(?!.*-\d*EP)", 100.0),
    (r"(?:SOIC|(?<![A-Z])SOP)[-_]?16(?![\d.x])", 80.0),
    (r"(?:SOIC|(?<![A-Z])SOP)[-_]?8(?![\d.x])", 120.0),
    (r"SOIC|SOP", 100.0),
    (r"BGA.*(?:256|324|400)", 20.0),
    (r"BGA", 25.0),
    (r"2512", 40.0),
    (r"2010", 60.0),
    (r"1210", 80.0),
    (r"1206", 100.0),
    (r"0805", 150.0),
    (r"0603", 200.0),
    (r"0402", 250.0),
    (r"0201", 350.0),
]
_COMPILED = [(re.compile(p, re.I), v) for p, v in PACKAGE_RTH_JA]
_EP = re.compile(r"(?<![A-Za-z])\d*EP(?![A-Za-z])|ExposedPad|EPAD|PowerPAD|ThermalVias", re.I)


def classify(footprint: str) -> dict | None:
    """{package, rth_ja, exposed_pad} for a footprint lib_id or name, or None
    when the package is not in the table."""
    name = (footprint or "").split(":")[-1]
    if not name:
        return None
    for pat, rth in _COMPILED:
        m = pat.search(name)
        if m:
            return {"package": m.group(0), "rth_ja": rth,
                    "exposed_pad": bool(_EP.search(name))}
    return None
