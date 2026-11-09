"""Structured datasheet specs, extracted by the calling agent and cached per
project.

The server does not run a language model. `task()` hands the agent the PDF
path, the most relevant pages' text, the schematic context and the schema;
the agent reads the datasheet and returns a spec record; `validate()` checks
it and `save()` writes `<project>/datasheets/specs/<MPN>.json`. Analyzers read
it with `load()` and must cite `page` when they use a value.

Spec record (schema version 1):

    {"mpn": "LT8471IUFD#TRPBF", "category": "regulator_switching",
     "description": "...",
     "pins": [{"number": "1", "name": "FB1", "description": "..."}],
     "absolute_max": {"VIN": SV, ...},
     "recommended":  {"VIN": SV, ...},
     "electrical":   {"IQ": SV, ...},
     "regulator":    {"vref": SV, "en_threshold_rising": SV, "en_active": "high",
                      "pg_output": "open_drain", "fsw": SV, "dropout": SV, ...,
                      "vref_negative": SV (inverting feedback, e.g. LT8471),
                      "anyout": {"base": SV, "pins": {"12": SV, ...}}
                        (pin-strapped output: Vout = base + each listed pin
                        that is tied to ground; keys are pin NUMBERS),
                      "channels": {"1": {"vref": SV}, "2": {...}}},
     "i2c":          {"addresses": ["0x48"], "address_rule": "...", "page": 12},
     "thermal":      {"rth_ja": SV, "tj_max": SV},
     "curves":       {"C_bias": CURVE, ...},
     "notes": ["..."]}

CURVE (a graph read off the datasheet): {"x_unit": unit, "y_unit": unit,
"points": [[x, y], ...] (>= 2, x ascending), "page": 1-based PDF page,
"conditions": str}. `C_bias`: capacitance vs DC bias, x in "V", y in "%"
of nominal (100 at 0 V) or in "F". Points are read from the plot, so
they are not checked against the page text.

Conventional keys the analyzers read (use these names):
  - capacitor: electrical.C, ESR, ESL, SRF; absolute_max.V (rated
    voltage); curves.C_bias;
  - inductor / ferrite: electrical.L, DCR, Isat, Irms (or Irms_<n>C, the
    lowest-rise one is used), SRF; ferrite Z_100MHz;
  - common_mode_choke: electrical.Z_cm (at a stated frequency), DCR,
    I_rated; absolute_max.V;
  - led: electrical.VF (at the IF in conditions), absolute_max.IF;
  - current_regulator (CCR): electrical.I_reg, V_min (anode-cathode
    voltage needed to regulate); absolute_max.V;
  - connector: electrical.I_rated (per contact) and / or I_rated_total
    (collective for the contacts of one rail, e.g. USB-C VBUS pins),
    absolute_max.V;
  - every part with a die: thermal.rth_ja, thermal.tj_max;
  - opamp: electrical.GBW, SR; crystal: electrical.CL, ESR.

SV (spec value): {"min", "typ", "max": number|null (at least one), "unit":
SI base unit from UNITS, "page": 1-based PDF page, "conditions": str,
"quote": verbatim text from that page (optional, checked), "between":
[pos pin, neg pin] for a pin-to-pin voltage — VDS ["D", "S"], VGS ["G",
"S"], diode reverse / TVS standoff ["K", "A"]; names from the datasheet pin
table, channel digits matched (D1-S1)}. Values are in SI
base units: 500 kHz -> 500000 "Hz", 0.8 V -> 0.8 "V", 75 mV -> 0.075 "V".

Checks: structure, units, min <= typ <= max, page within the PDF, and each
number (at any SI scale: 0.075 V found as "75") present in the cited page's
text — a miss is a warning (`unverified`), not a rejection, because tables
and graphs do not always extract as text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kicad_claude.adapters import datasheet_index as dsi

SCHEMA_VERSION = 1
SPECS_DIR = "specs"
UNITS = {"V", "A", "W", "Hz", "s", "Ohm", "F", "H", "C", "C/W", "%", "dB", "V/s",
         "A/s", "V/V", "A/V", "ppm/C", "", "bit", "B", "J", "Wb", "T", "m", "g"}
CATEGORIES = ("regulator_switching", "regulator_linear", "voltage_reference",
              "opamp", "comparator", "adc", "dac", "codec", "mcu", "interface",
              "pd_controller", "load_switch", "mosfet", "bjt", "diode", "tvs",
              "zener", "led", "current_regulator", "inductor", "common_mode_choke",
              "ferrite", "capacitor", "resistor", "crystal", "oscillator",
              "connector", "other")
SV_GROUPS = ("absolute_max", "recommended", "electrical")
REGULATOR_ENUMS = {"en_active": {"high", "low"},
                   "pg_output": {"open_drain", "push_pull", "none"}}
# Section headings that hold the tables (weight 10) and softer hints (1).
PAGE_HEADINGS = ("absolute maximum", "recommended operating", "electrical characteristics",
                 "pin configuration", "pin functions", "pin description", "pin assignment",
                 "ordering information", "thermal information", "i2c address",
                 "slave address", "device comparison")
PAGE_HINTS = ("feedback", "output voltage", "reference voltage", "enable threshold")
_SV_KEYS = {"min", "typ", "max", "unit", "page", "conditions", "quote", "between"}


def safe_name(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", mpn)


def specs_path(ds_dir: Path, mpn: str) -> Path:
    return ds_dir / SPECS_DIR / f"{safe_name(mpn)}.json"


# ---- task ---------------------------------------------------------------- #


def select_pages(pages: list[str], limit: int = 8) -> list[int]:
    """1-based pages worth reading first: page 1 plus keyword hits, ranked."""
    scored = []
    for i, t in enumerate(pages, 1):
        low = t.lower()
        s = 10 * sum(k in low for k in PAGE_HEADINGS) + min(5, sum(low.count(k) for k in PAGE_HINTS))
        if i == 1:
            s += 100
        if s:
            scored.append((s, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return sorted(i for _, i in scored[:limit])


def schema_text() -> str:
    return __doc__.split("Spec record (schema version 1):", 1)[1].split("Checks:", 1)[0].strip()


# ---- validation ------------------------------------------------------------ #


def _num_forms(v: float) -> set[str]:
    forms = set()
    for k in range(-4, 5):
        x = v * (1000 ** k)
        for s in (f"{x:g}", f"{x:.1f}", f"{x:.2f}", f"{x:.3f}"):
            s = s.rstrip("0").rstrip(".") if "." in s else s
            if s not in ("0", "-0", ""):
                forms.add(s.lstrip("-"))
    return forms


def _number_on_page(v: float, page_text: str) -> bool:
    if v == 0:
        return True
    text = page_text.replace("−", "-").replace(",", ".")
    nums = set(re.findall(r"\d+(?:\.\d+)?", text))
    # Table cells sometimes extract digit-spaced ("3 0" for 30).
    nums |= set(re.findall(r"\d+(?:\.\d+)?", re.sub(r"(?<=\d) (?=\d)", "", text)))
    nums |= {n.rstrip("0").rstrip(".") for n in nums if "." in n}   # 26.70 -> 26.7
    nums |= {n.lstrip("0") or "0" for n in nums}
    nums |= {("0" + n if n.startswith(".") else n) for n in nums}
    return bool(_num_forms(v) & nums)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _check_sv(path: str, sv, pages: list[str], errors: list, warnings: list) -> None:
    if not isinstance(sv, dict):
        errors.append(f"{path}: expected an object {{min, typ, max, unit, page}}")
        return
    extra = set(sv) - _SV_KEYS
    if extra:
        errors.append(f"{path}: unknown keys {sorted(extra)}")
    vals = {k: sv.get(k) for k in ("min", "typ", "max")}
    for k, v in vals.items():
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
            errors.append(f"{path}.{k}: must be a number or null")
            return
    present = [v for v in vals.values() if v is not None]
    if not present:
        errors.append(f"{path}: needs at least one of min / typ / max")
        return
    seq = [vals[k] for k in ("min", "typ", "max") if vals[k] is not None]
    if seq != sorted(seq):
        errors.append(f"{path}: min <= typ <= max violated ({vals})")
    bt = sv.get("between")
    if bt is not None and (not isinstance(bt, list) or len(bt) != 2
                           or not all(isinstance(x, str) and x.strip() for x in bt)):
        errors.append(f"{path}.between: [positive pin name, negative pin name]")
    unit = sv.get("unit")
    if unit not in UNITS:
        errors.append(f"{path}.unit: {unit!r} is not an SI base unit from {sorted(UNITS)}")
    page = sv.get("page")
    if not isinstance(page, int) or isinstance(page, bool):
        errors.append(f"{path}.page: required, 1-based integer")
        return
    if pages and not 1 <= page <= len(pages):
        errors.append(f"{path}.page: {page} outside 1..{len(pages)}")
        return
    if not pages:
        return
    text = pages[page - 1]
    missing = [v for v in present if not _number_on_page(float(v), text)]
    if missing and text.strip():
        warnings.append(f"{path}: value(s) {missing} not found in page {page} text "
                        "(unverified — check the page; tables/graphs may not extract)")
    q = sv.get("quote")
    if q and _norm_ws(q) not in _norm_ws(text):
        warnings.append(f"{path}.quote: not found verbatim on page {page}")


def validate(rec: dict, pages: list[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(rec, dict):
        return ["extraction must be a JSON object"], []
    allowed = {"mpn", "category", "description", "pins", *SV_GROUPS, "regulator",
               "i2c", "thermal", "curves", "notes"}
    extra = set(rec) - allowed - {"schema_version", "pdf", "extracted", "extracted_by"}
    if extra:
        errors.append(f"unknown top-level keys {sorted(extra)}; allowed {sorted(allowed)}")
    if rec.get("category") not in CATEGORIES:
        errors.append(f"category: {rec.get('category')!r} not in {list(CATEGORIES)}")
    pins = rec.get("pins", [])
    if not isinstance(pins, list) or any(
            not isinstance(p, dict) or not str(p.get("number", "")).strip() for p in pins):
        errors.append("pins: list of {number, name, description}; number required")
    for g in SV_GROUPS + ("thermal",):
        grp = rec.get(g, {})
        if not isinstance(grp, dict):
            errors.append(f"{g}: must be an object of name -> spec value")
            continue
        for name, sv in grp.items():
            _check_sv(f"{g}.{name}", sv, pages, errors, warnings)
    reg = rec.get("regulator")
    if reg is not None:
        if not isinstance(reg, dict):
            errors.append("regulator: must be an object")
        else:
            blocks = [("regulator", {k: v for k, v in reg.items() if k != "channels"})]
            for ch, body in (reg.get("channels") or {}).items():
                if not isinstance(body, dict):
                    errors.append(f"regulator.channels.{ch}: must be an object")
                    continue
                blocks.append((f"regulator.channels.{ch}", body))
            for prefix, body in blocks:
                for k, v in body.items():
                    if k == "anyout":
                        if not isinstance(v, dict) or not isinstance(v.get("pins"), dict):
                            errors.append(f"{prefix}.anyout: {{base: SV, pins: {{pin number: SV}}}}")
                            continue
                        _check_sv(f"{prefix}.anyout.base", v.get("base"), pages, errors, warnings)
                        for num, sv in v["pins"].items():
                            _check_sv(f"{prefix}.anyout.pins.{num}", sv, pages, errors, warnings)
                    elif k in REGULATOR_ENUMS:
                        if v not in REGULATOR_ENUMS[k]:
                            errors.append(f"{prefix}.{k}: {v!r} not in {sorted(REGULATOR_ENUMS[k])}")
                    else:
                        _check_sv(f"{prefix}.{k}", v, pages, errors, warnings)
    i2c = rec.get("i2c")
    if i2c is not None:
        if not isinstance(i2c, dict) or not isinstance(i2c.get("page"), int):
            errors.append("i2c: object with addresses, address_rule, page (int)")
        else:
            for a in i2c.get("addresses", []):
                if not re.fullmatch(r"0x[0-7][0-9A-Fa-f]", str(a)):
                    errors.append(f"i2c.addresses: {a!r} is not a 7-bit hex address (0x00-0x7F)")
    curves = rec.get("curves", {})
    if not isinstance(curves, dict):
        errors.append("curves: object of name -> CURVE")
        curves = {}
    for name, cv in curves.items():
        p = f"curves.{name}"
        if not isinstance(cv, dict):
            errors.append(f"{p}: expected {{x_unit, y_unit, points, page, conditions}}")
            continue
        extra = set(cv) - {"x_unit", "y_unit", "points", "page", "conditions"}
        if extra:
            errors.append(f"{p}: unknown keys {sorted(extra)}")
        for k in ("x_unit", "y_unit"):
            if cv.get(k) not in UNITS:
                errors.append(f"{p}.{k}: {cv.get(k)!r} is not a unit from {sorted(UNITS)}")
        pts = cv.get("points")
        if (not isinstance(pts, list) or len(pts) < 2 or any(
                not isinstance(q, list) or len(q) != 2
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in q)
                for q in pts)):
            errors.append(f"{p}.points: list of >= 2 [x, y] number pairs")
        elif [q[0] for q in pts] != sorted(q[0] for q in pts):
            errors.append(f"{p}.points: x must be ascending")
        page = cv.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or (
                pages and not 1 <= page <= len(pages)):
            errors.append(f"{p}.page: required, 1-based integer within the PDF")
    notes = rec.get("notes", [])
    if not isinstance(notes, list) or any(not isinstance(n, str) for n in notes):
        errors.append("notes: list of strings")
    return errors, warnings


# ---- store ---------------------------------------------------------------- #


def save(ds_dir: Path, mpn: str, rec: dict, pdf_file: str, sha: str, by: str) -> Path:
    out = dict(rec)
    out.update({"schema_version": SCHEMA_VERSION, "mpn": mpn,
                "pdf": {"file": pdf_file, "sha256": sha},
                "extracted": dsi.now(), "extracted_by": by})
    p = specs_path(ds_dir, mpn)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def load(ds_dir: Path, mpn: str) -> dict | None:
    """The cached record, with `stale: true` when the PDF changed since."""
    p = specs_path(ds_dir, mpn)
    if not p.is_file():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    pdf = ds_dir / rec.get("pdf", {}).get("file", "")
    rec["stale"] = not pdf.is_file() or dsi.sha256(pdf) != rec["pdf"].get("sha256")
    return rec


def value(sv: dict | None, prefer: str = "typ") -> float | None:
    if not sv:
        return None
    for k in (prefer, "typ", "min", "max"):
        if sv.get(k) is not None:
            return float(sv[k])
    return None


def curve_at(cv: dict, x: float) -> float:
    """Linear interpolation of a CURVE at x (clamped to its ends)."""
    pts = cv["points"]
    if x <= pts[0][0]:
        return float(pts[0][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0)) if x1 != x0 else float(y1)
    return float(pts[-1][1])


def c_bias_ratio(rec: dict | None, volts: float, nominal_f: float | None = None) -> float | None:
    """Remaining fraction of nominal capacitance at `volts` DC bias from the
    record's `curves.C_bias`, or None without a curve. A curve in F needs
    the nominal value."""
    cv = ((rec or {}).get("curves") or {}).get("C_bias")
    if not cv:
        return None
    y = curve_at(cv, abs(volts))
    if cv.get("y_unit") == "%":
        return y / 100.0
    if cv.get("y_unit") == "F" and nominal_f:
        return y / nominal_f
    return None


def rated(rec: dict | None, group: str, *keys: str, prefer: str = "max") -> tuple | None:
    """(|value|, page, key) of the first present key in `group` (magnitude:
    a P-channel FET's ID -6 A is a 6 A rating); for `Irms` the lowest of
    the Irms* keys (smallest temperature rise)."""
    grp = (rec or {}).get(group) or {}
    for k in keys:
        if k == "Irms":
            cands = [(abs(value(sv, "typ")), sv.get("page"), name) for name, sv in grp.items()
                     if name.lower().startswith("irms") and value(sv, "typ") is not None]
            if cands:
                return min(cands)
            continue
        sv = grp.get(k)
        v = value(sv, prefer)
        if v is not None:
            return abs(v), sv.get("page"), k
    return None
