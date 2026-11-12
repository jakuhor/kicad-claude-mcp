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
                      "t_ss": SV (soft-start time, s),
                      "light_load": "fccm" | "pfm" | "dcm" | "auto" (forced
                        PWM keeps the inductor in CCM at any load),
                      "input_pin": "C2" (the pin the channel's power flows
                        in through, when it is not the VIN pin - LT8471 ch2
                        draws from C2; VIN2 is only its driver supply),
                      "internal_outputs": {"SHOUT": {"above_pin": "C2",
                        "offset": SV}} (a pin the part drives at another
                        pin's voltage + offset, e.g. a Skyhook / bootstrap
                        rail),
                      "channels": {"1": {"vref": SV}, "2": {...}}},
     "i2c":          {"addresses": ["0x48"], "address_rule": "...", "page": 12},
     "thermal":      {"rth_ja": SV, "tj_max": SV, "psi_jb": SV},
     "curves":       {"C_bias": CURVE, ...},
     "pin_models":   {"OV/UV": {"clamp": SV, "i_trip": SV, "i_bias": SV,
                                "r_in": SV}, "EN_OUT": {"i_sink": SV,
                                "i_source": SV, "v_oh": SV}, "VDDD":
                                {"v_out": SV}, ...},
     "notes": ["..."]}

CURVE (a graph read off the datasheet): {"x_unit": unit, "y_unit": unit,
"points": [[x, y], ...] (>= 2, x ascending), "page": 1-based PDF page,
"conditions": str}. `C_bias`: capacitance vs DC bias, x in "V", y in "%"
of nominal (100 at 0 V) or in "F". Points are read from the plot, so
they are not checked against the page text.

PIN MODEL (keyed by the datasheet pin name, used by the DC operating-point
model): `clamp` internal clamp voltage to ground (V), `i_trip` current into
that clamp at which the part changes state (A, e.g. an OVLO threshold),
`i_bias` pin bias current, positive INTO the pin (A, max is used),
`r_in` input resistance to ground (Ohm), `i_sink` / `i_source` an output's
drive current (A; the minimum is used: above it the pin is a current
source), `v_oh` output-high level (V, minimum used), `v_out` a pin the part
holds at a fixed voltage (V, e.g. an internal LDO output). EN pull-up currents already in
`regulator.en_pullup_current` are applied without a pin model.

Conventional keys the analyzers read (use these names):
  - capacitor: electrical.C, ESR, ESL, SRF; absolute_max.V (rated
    voltage); curves.C_bias;
  - inductor / ferrite: electrical.L, DCR, Isat, Irms (or Irms_<n>C, the
    lowest-rise one is used), SRF; ferrite Z_100MHz;
  - common_mode_choke: electrical.Z_cm (at a stated frequency), DCR,
    I_rated; absolute_max.V;
  - led: electrical.VF (at the IF in conditions), absolute_max.IF;
    curves.VF (x IF in "A", y VF in "V") for VF at the operating current;
  - switching regulator: curves.efficiency (x output current "A", y "%",
    conditions "VIN = 12 V, VOUT = 5 V"; several curves allowed, the closest
    VIN / VOUT is used; `efficiency_ch2` for one channel);
  - current_regulator (CCR): electrical.I_reg, V_min (anode-cathode
    voltage needed to regulate); absolute_max.V;
  - connector: electrical.I_rated (per contact) and / or I_rated_total
    (collective for the contacts of one rail, e.g. USB-C VBUS pins),
    absolute_max.V;
  - fuse / PTC: electrical.I_hold (PTC: largest current that never
    trips), I_trip (PTC: smallest current that always trips), I_rated
    (fuse); absolute_max.V;
  - every part with a die: thermal.rth_ja, thermal.tj_max; psi_jb (or
    rth_jb) when the thermal table gives it (board temperature next to it);
  - opamp: electrical.GBW, SR, Aol (dB or V/V; with GBW it gives the
    behavioural model when there is no vendor model), Vos, R_in;
  - LDO / reference behavioural model: regulator.vref, dropout,
    electrical.IQ, PSRR, Z_out;
  - crystal: electrical.CL, ESR.

SV (spec value): {"min", "typ", "max": number|null (at least one), "unit":
SI base unit from UNITS, "page": 1-based PDF page, "conditions": str,
"quote": verbatim text from that page (optional, checked), "between":
[pos pin, neg pin] for a pin-to-pin voltage — VDS ["D", "S"], VGS ["G",
"S"], diode reverse / TVS standoff ["K", "A"]; names from the datasheet pin
table, channel digits matched (D1-S1)}. Values are in SI
base units: 500 kHz -> 500000 "Hz", 0.8 V -> 0.8 "V", 75 mV -> 0.075 "V".

Checks: structure, units, min <= typ <= max, page within the PDF, and each
number present in the cited page's text with its sign, decimal point and
unit: 0.075 V is found as "75 mV" or "0.075 V" (another scale only with that
scale's unit; a bare table cell only at base scale), -40 needs its minus,
"±15 V" satisfies only a min -15 / max 15 pair. A miss is a warning
(`unverified`), not a rejection, because tables and graphs do not always
extract as text.
"""

from __future__ import annotations

import json
import math
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
              "connector", "fuse", "other")
SV_GROUPS = ("absolute_max", "recommended", "electrical")
REGULATOR_ENUMS = {"en_active": {"high", "low"},
                   "light_load": {"fccm", "pfm", "dcm", "auto"},
                   "pg_output": {"open_drain", "push_pull", "none"}}
_SV_KEYS = {"min", "typ", "max", "unit", "page", "conditions", "quote", "between"}


def safe_name(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", mpn)


def specs_path(ds_dir: Path, mpn: str) -> Path:
    return ds_dir / SPECS_DIR / f"{safe_name(mpn)}.json"


# ---- task ---------------------------------------------------------------- #


# Page selection. Section patterns, the table-of-contents scan and the
# keyword hints follow kicad-happy 3.0.1 (MIT, © 2025 Andrew Klofas):
# datasheets/scripts/datasheet_page_selector.py:_find_toc_pages / _score_page.
SECTIONS = {
    "pinout": r"pin\s+(?:configuration|functions?|descriptions?|assignments?|definitions?|"
              r"diagram)|pin\s*out\b",
    "abs_max": r"absolute\s+maximum\s+rat",
    "recommended": r"recommended\s+operating\s+cond",
    "electrical": r"electrical\s+characteristics?",
    "thermal": r"thermal\s+(?:information|characteristics|resistance|metrics)|"
               r"r\s*[θΘ]\s*ja|theta\s*[-_]?\s*ja",
    "application": r"typical\s+applications?|application\s+(?:circuit|information|schematic)",
}
SECTION_ORDER = ("pinout", "abs_max", "recommended", "electrical", "thermal", "application")
_OTHER_HEADINGS = r"ordering\s+information|i2c\s+address|slave\s+address|device\s+comparison"
_HINTS = {
    "electrical": r"\b(?:min|typ|max)\b|feedback|reference\s+voltage|quiescent|dropout|"
                  r"switching\s+frequency|enable\s+threshold|gain[- ]?bandwidth|slew\s+rate",
    "pinout": r"\bpin\b.*\b(?:name|no\.|number|function)\b",
    "application": r"\binductor\s+selection\b|\boutput\s+capacitor\b|\bschematic\b",
    "recommended": r"\boperating\s+(?:temperature|voltage)\b",
}


def _section_scores(text: str) -> dict[str, float]:
    low = text.lower()
    out = {}
    for sec, pat in SECTIONS.items():
        n = len(re.findall(pat, low))
        h = len(re.findall(_HINTS[sec], low)) if sec in _HINTS else 0
        sc = 10.0 * min(n, 2) + min(h, 5) * 0.5
        if sc:
            out[sec] = sc
    if re.search(_OTHER_HEADINGS, low):
        out["other"] = 10.0
    return out


def find_toc(pages: list[str], scan: int = 3) -> dict[int, str]:
    """{PDF page: section} from a table of contents in the first `scan`
    pages ("Absolute Maximum Ratings ....... 4"). A printed page number is
    moved to the nearest PDF page (n-1 .. n+3) that names the section, so a
    cover-page offset does not misplace it."""
    out: dict[int, str] = {}
    for text in pages[:scan]:
        low = text.lower()
        for sec, pat in SECTIONS.items():
            for m in re.finditer(r"(?:" + pat + r")[^\n\d]{0,60}?[\s.…·_-]{2,}(\d{1,3})\b", low):
                n = int(m.group(1))
                if not 1 <= n <= len(pages) + 3:
                    continue
                cands = [k for k in (n, n + 1, n + 2, n + 3, n - 1)
                         if 1 <= k <= len(pages) and k > scan // 2
                         and re.search(pat, pages[k - 1].lower())]
                k = cands[0] if cands else (n if n <= len(pages) else None)
                if k:
                    out.setdefault(k, sec)
    return out


def page_reasons(pages: list[str], limit: int = 8) -> dict[int, str]:
    """{1-based page: reason} for the pages worth reading first: page 1, the
    table-of-contents targets, the best page of each section (pinout, abs
    max, recommended, electrical, thermal, application), then the highest
    scoring pages up to `limit`."""
    if not pages:
        return {}
    chosen: dict[int, str] = {1: "front page"}
    toc = find_toc(pages)
    for sec in SECTION_ORDER + ("other",):
        for k, s_ in sorted(toc.items()):
            if s_ == sec and len(chosen) < limit:
                chosen.setdefault(k, f"table of contents: {sec}")
    scores = {i: _section_scores(t) for i, t in enumerate(pages, 1)}
    for sec in SECTION_ORDER:
        if any(r.endswith(sec) for r in chosen.values()):
            continue
        best = max(((sc.get(sec, 0.0), -i, i) for i, sc in scores.items()), default=(0, 0, 0))
        if best[0] >= 10 and len(chosen) < limit:
            chosen.setdefault(best[2], f"heading: {sec}")
    rest = sorted(((sum(sc.values()), i) for i, sc in scores.items() if i not in chosen and sc),
                  key=lambda x: (-x[0], x[1]))
    for total, i in rest:
        if len(chosen) >= limit:
            break
        top = max(scores[i], key=scores[i].get)
        chosen[i] = f"keywords: {top} ({total:g})"
    return dict(sorted(chosen.items()))


def select_pages(pages: list[str], limit: int = 8) -> list[int]:
    """1-based pages worth reading first (see `page_reasons`)."""
    return list(page_reasons(pages, limit))


def schema_text() -> str:
    return __doc__.split("Spec record (schema version 1):", 1)[1].split("Checks:", 1)[0].strip()


# ---- validation ------------------------------------------------------------ #


# Value check: the number must be on the cited page with its sign, decimal
# point and unit. Behaviour follows kicad-happy's Deep Review quote gate.
# Ported from kicad-happy 3.0.1 (MIT, © 2025 Andrew Klofas):
# kicad/review/scripts/deep_review_gate.py:_norm_text / _quote_in_text
_DASHES = re.compile("[‐‑‒–—―−﹣－]")
_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
           "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
# printed symbol -> SI base unit of UNITS
_SYMBOL = {"V": "V", "A": "A", "W": "W", "Hz": "Hz", "s": "s", "sec": "s", "Ω": "Ohm",
           "ohm": "Ohm", "ohms": "Ohm", "Ohm": "Ohm", "F": "F", "H": "H", "°C": "C",
           "C": "C", "%": "%", "dB": "dB", "bit": "bit", "bits": "bit", "B": "B", "J": "J",
           "Wb": "Wb", "T": "T", "m": "m", "g": "g", "ppm": "ppm"}
_DEN = {"°C": "C", "C": "C", "K": "C", "W": "W", "V": "V", "A": "A", "s": "s"}
_NUM_RE = re.compile(r"(?<![\w.])(±)?(-)?(\d+(?:\.\d+)?|\.\d+)(?!\d)")
_TOK_RE = re.compile(r"\s*([A-Za-zµμΩ°%][\w/°µμΩ%]*|\d+(?:\.\d+)?|\.\d+|[^\s\w])")
_SKIP_WORDS = {"to", "and", "or"}


def _norm_text(text: str) -> str:
    """Page text with Unicode variants, dashes, thousands separators and
    line-wrap hyphenation normalised (NFKC: ℃ -> °C, Ω sign -> Ω)."""
    import unicodedata

    t = unicodedata.normalize("NFKC", text)
    t = _DASHES.sub("-", t)
    t = re.sub(r"(?<=[A-Za-z])-[ \t]*\r?\n\s*(?=[a-z])", "", t)    # over-\nvoltage
    t = re.sub(r"±\s+", "±", t)                                   # "± 15 V"
    t = re.sub(r"°\s+C\b", "°C", t)
    t = re.sub(r"\b(\d{1,3})((?:,\d{3})+)(?![\d,])",
               lambda m: m.group(1) + m.group(2).replace(",", ""), t)  # 1,500
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)                           # 5,5 V
    return t


def _unit_scale(tok: str) -> tuple[float, str] | None:
    """(scale to SI, base unit) of a printed unit token, e.g. mV -> (1e-3, V),
    V/µs -> (1e6, V/s), K/W -> (1, C/W); None when it is not a unit."""
    def one(t: str, table: dict) -> tuple[float, str] | None:
        if t in table:
            return 1.0, table[t]
        if len(t) > 1 and t[0] in _PREFIX and t[1:] in table:
            return _PREFIX[t[0]], table[t[1:]]
        return None
    if "/" in tok:
        num, _, den = tok.partition("/")
        a, b = one(num, {**_SYMBOL, "K": "C"}), one(den, _DEN)
        if not a or not b:
            return None
        return a[0] / b[0], f"{a[1]}/{b[1]}"
    return one(tok, _SYMBOL)


def _printed_unit(text: str, pos: int) -> tuple[float, str] | None:
    """The unit a number ending at `pos` is printed in: the first unit token
    after it in the same row (other numbers, dashes and 'to' skipped), or
    None when a word or the end comes first (a bare table cell)."""
    for _ in range(8):
        m = _TOK_RE.match(text, pos)
        if not m:
            return None
        tok, pos = m.group(1), m.end()
        if tok[0].isdigit() or tok[0] == "." or tok.lower() in _SKIP_WORDS:
            continue
        if tok[0] in "°%" or tok[0].isalpha() or tok[0] in "µμΩ":
            return _unit_scale(tok)
        if tok in "-~±()[],;:|":
            continue
        return None
    return None


def _value_on_page(v: float, unit: str, page_text: str, symmetric: bool = False) -> bool:
    """True when `v` (SI `unit`) is printed on the page with its sign, decimal
    point and unit: 0.075 V as `75 mV` or `0.075 V` (a bare `0.075` cell
    counts only at base scale), −40 as `-40` / `−40`, a `±15 V` only for a
    symmetric min / max pair (`symmetric`). Digit-spaced cells (`3 0 V`)
    are read joined too."""
    if v == 0:
        return True
    text = _norm_text(page_text)
    for t in (text, re.sub(r"(?<=\b\d) (?=\d\b)", "", text)):
        for m in _NUM_RE.finditer(t):
            pm, neg, n = bool(m.group(1)), bool(m.group(2)), float(m.group(3))
            if v < 0 and not (neg or (pm and symmetric)):
                continue
            if v > 0 and (neg or (pm and not symmetric)):
                continue
            pu = _printed_unit(t, m.end())
            if unit == "" or pu is None:
                scale = 1.0
            elif pu[1] == unit:
                scale = pu[0]
            else:
                continue
            if math.isclose(n * scale, abs(v), rel_tol=1e-6):
                return True
    return False


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", _norm_text(s)).strip().lower()


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
    symmetric = (vals["min"] is not None and vals["max"] is not None
                 and vals["min"] == -vals["max"])
    missing = [v for v in present
               if not _value_on_page(float(v), unit or "", text, symmetric)]
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
               "i2c", "thermal", "curves", "pin_models", "notes"}
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
                    elif k == "input_pin":
                        if not isinstance(v, str) or not v.strip():
                            errors.append(f"{prefix}.input_pin: a pin name")
                    elif k == "internal_outputs":
                        if not isinstance(v, dict):
                            errors.append(f"{prefix}.internal_outputs: {{pin: {{above_pin, offset}}}}")
                            continue
                        for pin, io in v.items():
                            if not isinstance(io, dict) or not isinstance(io.get("above_pin"), str):
                                errors.append(f"{prefix}.internal_outputs.{pin}: "
                                              "{above_pin: pin name, offset: SV}")
                                continue
                            _check_sv(f"{prefix}.internal_outputs.{pin}.offset", io.get("offset"),
                                      pages, errors, warnings)
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
    pms = rec.get("pin_models", {})
    if not isinstance(pms, dict):
        errors.append("pin_models: object of pin name -> {clamp, i_trip, i_bias, r_in, "
                      "i_sink, i_source, v_oh, v_out}")
        pms = {}
    units = {"clamp": "V", "i_trip": "A", "i_bias": "A", "r_in": "Ohm", "i_sink": "A",
             "i_source": "A", "v_oh": "V", "v_out": "V"}
    for pin, m in pms.items():
        if not isinstance(m, dict) or not m or set(m) - set(units):
            errors.append(f"pin_models.{pin}: keys from {sorted(units)}")
            continue
        for k, sv in m.items():
            _check_sv(f"pin_models.{pin}.{k}", sv, pages, errors, warnings)
            if isinstance(sv, dict) and sv.get("unit") != units[k]:
                errors.append(f"pin_models.{pin}.{k}.unit: must be {units[k]!r}")
    notes = rec.get("notes", [])
    if not isinstance(notes, list) or any(not isinstance(n, str) for n in notes):
        errors.append("notes: list of strings")
    return errors, warnings


# ---- quality ---------------------------------------------------------------- #

# Keys the analyzers read, per category ("a|b": either). Mirrors the
# conventional keys in the schema above.
ANALYZER_KEYS = {
    "regulator_switching": ["regulator.vref|regulator.vref_negative|regulator.anyout",
                            "regulator.fsw", "recommended.VIN|absolute_max.VIN",
                            "recommended.IOUT|electrical.ILIM|electrical.ICL",
                            "regulator.en_threshold_rising|regulator.en_active"],
    "regulator_linear": ["regulator.vref|regulator.anyout|regulator.set_current",
                         "regulator.dropout", "recommended.VIN|absolute_max.VIN",
                         "recommended.IOUT", "electrical.IQ|electrical.I_GND"],
    "voltage_reference": ["regulator.vref", "recommended.VIN|absolute_max.VIN"],
    "opamp": ["electrical.GBW", "electrical.SR", "recommended.VS|absolute_max.VS|"
              "recommended.VCC|absolute_max.VCC"],
    "capacitor": ["electrical.C", "absolute_max.V|electrical.V_rated"],
    "inductor": ["electrical.L", "electrical.DCR", "electrical.Isat", "electrical.Irms*"],
    "ferrite": ["electrical.Z_100MHz", "electrical.DCR", "electrical.I_rated|electrical.Irms*"],
    "common_mode_choke": ["electrical.Z_cm", "electrical.DCR", "electrical.I_rated"],
    "led": ["electrical.VF", "absolute_max.IF"],
    "current_regulator": ["electrical.I_reg", "electrical.V_min"],
    "connector": ["electrical.I_rated|electrical.I_rated_total"],
    "fuse": ["electrical.I_hold|electrical.I_rated"],
    "crystal": ["electrical.CL", "electrical.ESR"],
    "mosfet": ["absolute_max.VDS", "electrical.RDS*", "electrical.ID|absolute_max.ID"],
    "diode": ["electrical.VF", "absolute_max.IF|electrical.IF|electrical.IF_AV",
              "absolute_max.VR|absolute_max.VRRM"],
    "tvs": ["absolute_max.VWM|recommended.VWM|electrical.VBR", "electrical.VC"],
    "zener": ["electrical.VZ"],
}
PASSIVE_CATEGORIES = {"capacitor", "resistor", "inductor", "ferrite", "common_mode_choke",
                      "connector", "fuse", "crystal"}
QUALITY_WEIGHTS = {"pins": 30, "ratings": 25, "electrical": 20, "thermal": 10, "analyzer_keys": 15}


def _has(rec: dict, path: str) -> bool:
    for alt in path.split("|"):
        group, _, key = alt.partition(".")
        grp = rec.get(group) or {}
        if key.endswith("*"):
            if any(k.upper().replace("(", "").replace(")", "").replace("_", "")
                   .startswith(key[:-1].upper().replace("_", "")) for k in grp):
                return True
        elif key in grp:
            return True
    return False


def quality(rec: dict, symbol_pins: int | None = None) -> dict:
    """0-100 completeness of a spec record with what is missing: pin table
    vs the symbol's pin count, voltage ratings (abs max, recommended, TJ),
    electrical characteristics, thermal (rth_ja, tj_max), and the keys the
    analyzers read for its category (`ANALYZER_KEYS`). Pins and thermal do
    not count for passives. Below 50: DQ-LOW."""
    cat = rec.get("category", "")
    parts, missing = {}, []
    passive = cat in PASSIVE_CATEGORIES
    if not passive:
        pins = [p for p in rec.get("pins") or [] if str(p.get("name", "")).strip()]
        if symbol_pins:
            parts["pins"] = min(1.0, len(pins) / symbol_pins)
            if len(pins) < symbol_pins:
                missing.append(f"pins: {len(pins)} of the symbol's {symbol_pins}")
        else:
            parts["pins"] = 1.0 if pins else 0.0
            if not pins:
                missing.append("pins: no pin table")
    am, ro = rec.get("absolute_max") or {}, rec.get("recommended") or {}
    r = 0.0
    if any((sv or {}).get("unit") == "V" for sv in am.values()):
        r += 0.5
    else:
        missing.append("absolute_max: no voltage rating")
    if passive or any((sv or {}).get("unit") == "V" for sv in ro.values()):
        r += 0.3
    else:
        missing.append("recommended: no operating voltage")
    if passive or _has(rec, "absolute_max.TJ|thermal.tj_max|recommended.T_A|recommended.TA"):
        r += 0.2
    else:
        missing.append("no TJ / temperature range")
    parts["ratings"] = r
    n_el = len(rec.get("electrical") or {}) + len(
        {k: v for k, v in (rec.get("regulator") or {}).items() if isinstance(v, dict)})
    parts["electrical"] = min(1.0, n_el / 3)
    if n_el < 3:
        missing.append(f"electrical: {n_el} value(s)")
    if not passive:
        th = rec.get("thermal") or {}
        parts["thermal"] = 0.5 * ("rth_ja" in th) + 0.5 * bool(
            "tj_max" in th or "TJ" in am)
        if "rth_ja" not in th:
            missing.append("thermal.rth_ja")
    keys = ANALYZER_KEYS.get(cat)
    if keys:
        have = [k for k in keys if _has(rec, k)]
        parts["analyzer_keys"] = len(have) / len(keys)
        missing += [f"analyzer key {k}" for k in keys if k not in have]
    w = {k: QUALITY_WEIGHTS[k] for k in parts}
    score = round(100 * sum(parts[k] * w[k] for k in parts) / sum(w.values())) if w else 0
    out = {"score": score, "parts": {k: round(v, 2) for k, v in parts.items()},
           "missing": missing}
    if score < 50:
        out["finding"] = {"severity": "info", "rule": "DQ-LOW", "ref": rec.get("mpn", ""),
                          "message": f"Extraction quality {score}/100: "
                                     + "; ".join(missing[:6]) + "."}
    return out


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
