"""Capacitor DC-bias and temperature curves fetched from the vendors' own
web tools, cached per project in `<project>/datasheets/curves/<MPN>.json`.

None of these endpoints is a documented public API: they are the JSON back
ends of each vendor's design tool (see docs, OP-52 research). A response
that does not parse is reported as `error: "format changed"`, never guessed
around. Only the MPN is sent.

  - Murata SimSurfing (measured): `ds.murata.com/simserve/characteristics`,
    DC bias at 25 °C and at other temperatures (`tc`), temperature curve.
  - Samsung Electro-Mechanics Component Library: the part's data-sheet page
    embeds its graphs as JSON (`var datasheetData = [...]`): DC bias and
    TCC at 25 °C.
  - KEMET K-SIM (calculated from KEMET's model): `ksim3.kemet.com` `/pn`
    then `/plot` (`Vbias`, `TCC`). K-SIM's Vbias does not change with the
    ambient temperature, so it gives no `C_bias_at`.

Stored curves use the spec-cache CURVE format, with y as the RETAINED
share in "%": `C_bias` 100 at 0 V (of nominal), `C_temp` 100 at 25 °C,
`C_bias_at[T]` the DC-bias curve measured at T °C (of nominal at 25 °C / 0 V,
so it includes the temperature effect). Vendors give the change rate
(0 at 0 V); it is converted on store.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

import httpx

from kicad_claude.adapters import datasheet_index as dsi

CURVES_DIR = "curves"
VENDORS = ("murata", "samsung", "kemet")
POINTS = 40              # stored points per curve (vendor curves have 60-500)
MIN_INTERVAL_S = 1.0     # between two requests to one host
DEFAULT_TEMPS_C = (85.0, 105.0, 125.0)

MURATA_URL = "https://ds.murata.com/simserve/characteristics"
SAMSUNG_URL = "https://weblib.samsungsem.com/mlcc/mlcc-ec-data-sheet.do"
KEMET_URL = "https://ksim3.kemet.com"
_UA = "Mozilla/5.0 (compatible; kicad-claude)"

_MURATA_RE = re.compile(r"^(GRM|GCM|GCJ|GJM|GRT|GCQ|GQM|GA2|GA3|GRJ|GCE|GCG|GXT|GMD|KCM|KC3|KRM|"
                        r"LLL|LLA|LLR|ZRA|ZRB|EVA)", re.I)
_SAMSUNG_RE = re.compile(r"^CL\d\d[A-Z]\d{3}", re.I)
_KEMET_RE = re.compile(r"^C(0201|0402|0603|0805|1206|1210|1808|1812|1825|2220|2225)"
                       r"[A-Z]\d{3}[A-Z][0-9A-Z]", re.I)


class FormatChanged(ValueError):
    """The vendor's response no longer has the expected shape."""


def vendor_for(mpn: str, manufacturer: str = "") -> str | None:
    """Vendor from the Manufacturer field, else from the MPN prefix."""
    m = manufacturer.lower()
    for v in VENDORS:
        if v in m:
            return v
    if "semco" in m or "electro-mechanics" in m:
        return "samsung"
    if manufacturer.strip():
        return None   # a named other maker: never guess from the prefix
    for v, rx in (("murata", _MURATA_RE), ("samsung", _SAMSUNG_RE), ("kemet", _KEMET_RE)):
        if rx.match(mpn.strip()):
            return v
    return None


# ---- curve helpers ------------------------------------------------------- #


def downsample(points: list, n: int = POINTS) -> list:
    """About `n` points, evenly by index, both ends kept."""
    if len(points) <= n:
        return points
    step = (len(points) - 1) / (n - 1)
    return [points[round(i * step)] for i in range(n)]


def _curve(xy: list, x_unit: str, conditions: str, source: str, method: str) -> dict:
    """CURVE from vendor (x, change %) pairs: sorted, deduplicated, retained %."""
    pts = sorted({float(x): float(y) for x, y in xy}.items())
    if len(pts) < 2:
        raise FormatChanged("curve with fewer than 2 points")
    pts = [[round(x, 4), round(100.0 + y, 3)] for x, y in downsample(pts)]
    return {"x_unit": x_unit, "y_unit": "%", "points": pts, "conditions": conditions,
            "source": source, "method": method}


class _Http:
    """httpx with a per-host pause; `get` / `post` return the response."""

    def __init__(self, timeout: float = 20.0):
        self.c = httpx.Client(timeout=timeout, headers={"User-Agent": _UA},
                              follow_redirects=True)
        self.last: dict[str, float] = {}

    def _wait(self, url: str):
        host = httpx.URL(url).host
        dt = time.monotonic() - self.last.get(host, -1e9)
        if dt < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - dt)
        self.last[host] = time.monotonic()

    def get(self, url, **kw):
        self._wait(url)
        try:
            return self.c.get(url, **kw)
        except httpx.RemoteProtocolError:   # server closed the kept-alive socket (Samsung after a 500)
            return self.c.get(url, **kw)

    def post(self, url, **kw):
        self._wait(url)
        return self.c.post(url, **kw)


# ---- Murata -------------------------------------------------------------- #


def _murata(http, params: dict):
    r = http.get(MURATA_URL, params={"callback": "nothing", "WorkInfo": "k", **params})
    r.raise_for_status()
    return r.json()


def _murata_chara(http, pn: str, chara: str, parameter: dict) -> dict:
    req = [{"partnumber": pn, "chara_type": chara, "parameter": parameter}]
    d = _murata(http, {"ReqType": "Characteristics", "ReqChara": json.dumps(req)})
    try:
        return d["JsonCharaData"][0]["charadata"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise FormatChanged(f"Murata {chara}: no charadata") from e


def _murata_xy(c: dict) -> list:
    try:
        return [(p[0][0], p[1][0]) for p in c["data"]]
    except (KeyError, IndexError, TypeError) as e:
        raise FormatChanged("Murata data is not [[[x],[y]], ...]") from e


def fetch_murata(http, mpn: str, temps_c) -> dict:
    info = _murata(http, {"ReqType": "GetProductInfo", "partnumbers": mpn})
    try:
        pn = info[0]["nopackingcode"]
    except (KeyError, IndexError, TypeError) as e:
        raise FormatChanged("Murata GetProductInfo") from e
    if not pn:
        return {"status": "not_found"}
    src = f"Murata SimSurfing {pn}"
    curves: dict = {}
    for chara, key, unit in (("c_dcbias_capchange", "C_bias", "V"),
                             ("c_temp_capchange", "C_temp", "C")):
        c = _murata_chara(http, pn, chara, {})
        if c.get("error"):
            continue
        curves[key] = _curve(_murata_xy(c), unit, c.get("caption2", ""), src, "measured")
    at = {}
    for t in temps_c:
        c = _murata_chara(http, pn, "c_dcbias_capchange", {"tc": f"{t:g}"})
        if c.get("error"):   # "Specified Temp. is out of range" past the dielectric's limit
            continue
        at[f"{t:g}"] = _curve(_murata_xy(c), "V", c.get("caption2", ""), src, "measured")
    if at:
        curves["C_bias_at"] = at
    return {"status": "fetched" if curves else "no_curves", "vendor_pn": pn,
            "source_url": f"https://ds.murata.com/simsurfing/mlcc.html?partnumbers=%5B%22{pn}%22%5D",
            "curves": curves}


# ---- Samsung ------------------------------------------------------------- #


def _samsung_blocks(html: str) -> list | None:
    i = html.find("var datasheetData = ")
    if i < 0:
        return None
    try:
        d, _ = json.JSONDecoder().raw_decode(html[i + len("var datasheetData = "):])
    except ValueError as e:
        raise FormatChanged("Samsung datasheetData is not JSON") from e
    return d


def parse_samsung(html: str, pn: str) -> dict:
    blocks = _samsung_blocks(html)
    if blocks is None:
        return {"status": "not_found"}
    src = f"Samsung Electro-Mechanics Component Library {pn}"
    curves: dict = {}
    try:
        for g in blocks:
            for sp in g["selectPartsList"]:
                for c in sp["chartList"]:
                    if c.get("asixYOption") != "DeltaC":
                        continue
                    xy = [(p["x"], p["y"]) for p in c["data"]]
                    cond = f"{sp.get('degC', 25):g} degC"
                    if g["graphType"] == "DCBias" and "C_bias" not in curves:
                        curves["C_bias"] = _curve(xy, "V", cond, src, "measured")
                    elif (g["graphType"] == "TCC" and c.get("graphSubType") == "TCC"
                          and "C_temp" not in curves):   # BiasTCC (at half the rating) skipped
                        curves["C_temp"] = _curve(xy, "C", f"DC {sp.get('vdc', 0):g} V", src,
                                                  "measured")
    except (KeyError, TypeError) as e:
        raise FormatChanged("Samsung graph block") from e
    return {"status": "fetched" if curves else "no_curves", "vendor_pn": pn,
            "source_url": f"{SAMSUNG_URL}?partNumber={pn}", "curves": curves}


def fetch_samsung(http, mpn: str, temps_c) -> dict:
    # the page wants the MPN without its packing letter (CL21A106KAYNNN, not ...NNNE)
    for pn in dict.fromkeys((mpn, mpn[:-1] if len(mpn) > 14 else mpn)):
        r = http.get(SAMSUNG_URL, params={"partNumber": pn})   # unknown form: 500
        if r.status_code == 200 and "var datasheetData" in r.text:
            return parse_samsung(r.text, pn)
    return {"status": "not_found"}


# ---- KEMET --------------------------------------------------------------- #


def _kemet_body(part: dict, plot: str, session: str) -> dict:
    # the shape K-SIM's own front end posts to /plot
    return {"calculated": "No", "combine": "No", "freqList": [1000], "highlight": 0, "ind": 0,
            "line": "50", "path": "Shunt", "parts": [part], "plotType": plot, "res": 0,
            "spiceFileFormat": "CKT", "spiceFreq": 1000, "start": 100, "stop": 1e9,
            "tempRise": 20, "session": session}


def _kemet_points(d: dict) -> list:
    try:
        return next(([(p["x"], p["y"]) for p in s["points"]]
                     for s in d["seriesList"] if s.get("points")), [])
    except (KeyError, TypeError) as e:
        raise FormatChanged("KEMET seriesList") from e


def fetch_kemet(http, mpn: str, temps_c) -> dict:
    session = uuid.uuid4().hex
    r = http.post(f"{KEMET_URL}/pn", json={"partNumber": mpn, "method": "search",
                                            "session": session})
    r.raise_for_status()
    part = r.json()
    if not isinstance(part, dict):   # "No match found."
        return {"status": "not_found"}
    if "kemetPn" not in part:
        raise FormatChanged("KEMET /pn record")
    pn = part["kemetPn"]
    src = f"KEMET K-SIM {pn}"
    curves: dict = {}
    for plot, key, unit in (("Vbias", "C_bias", "V"), ("TCC", "C_temp", "C")):
        r = http.post(f"{KEMET_URL}/plot", json=_kemet_body(part, plot, session))
        r.raise_for_status()
        xy = _kemet_points(r.json())
        if len(xy) < 2 or not any(abs(float(y)) > 1e-9 for _, y in xy):
            continue   # K-SIM answers all zeros when it has no data
        curves[key] = _curve(xy, unit, "1 kHz", src, "model")
    return {"status": "fetched" if curves else "no_curves", "vendor_pn": pn,
            "source_url": f"https://ksim3.kemet.com/?pn={pn}", "curves": curves}


FETCHERS = {"murata": fetch_murata, "samsung": fetch_samsung, "kemet": fetch_kemet}


def fetch(mpn: str, manufacturer: str = "", temps_c=DEFAULT_TEMPS_C, http=None) -> dict:
    """One MPN: {status, vendor, ...}. status: fetched, no_curves, not_found,
    unsupported, error."""
    vendor = vendor_for(mpn, manufacturer)
    if vendor is None:
        return {"mpn": mpn, "status": "unsupported",
                "hint": "only Murata, Samsung and KEMET are fetched; extract the curve from "
                        "the datasheet (curves.C_bias / C_temp) instead"}
    http = http or _Http()
    try:
        rec = FETCHERS[vendor](http, mpn, temps_c)
    except FormatChanged as e:
        return {"mpn": mpn, "vendor": vendor, "status": "error", "error": f"format changed: {e}"}
    except (httpx.HTTPError, ValueError) as e:
        return {"mpn": mpn, "vendor": vendor, "status": "error", "error": str(e)}
    return {"mpn": mpn, "vendor": vendor, **rec}


# ---- cache --------------------------------------------------------------- #


def curves_path(ds_dir: Path, mpn: str) -> Path:
    return ds_dir / CURVES_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', mpn)}.json"


def save(ds_dir: Path, rec: dict) -> Path:
    p = curves_path(ds_dir, rec["mpn"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({**rec, "fetched": dsi.now()}, indent=1, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    return p


def load(ds_dir: Path, mpn: str) -> dict | None:
    p = curves_path(ds_dir, mpn)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except ValueError:
        return None


def merge(spec: dict | None, fetched: dict | None) -> dict | None:
    """Spec record with the fetched curves added (vendor curves win over a
    curve read off a PDF plot); a curves-only record when there is no spec."""
    curves = (fetched or {}).get("curves")
    if not curves:
        return spec
    base = dict(spec) if spec else {"mpn": fetched["mpn"], "category": "capacitor",
                                    "curves_only": True}
    base["curves"] = {**(base.get("curves") or {}), **curves}
    return base
