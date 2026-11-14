"""Behavioural SPICE models built from a part's extracted datasheet specs.

Used when a stage has no vendor model. Only the spec record's values are
used (decided 2026-11-12: no distributor parametric data, no defaults): a
required key missing -> no model, and the caller says which keys. Each model
is labelled `behavioural, from datasheet <MPN> p.N`.

  op-amp  required GBW (Hz) and an open-loop gain (Aol / AOL / AVOL in dB or
          V/V); optional Vos (V), R_in (Ohm). Single pole at GBW / Aol, a
          linear small-signal model (no slew-rate or output-swing limits:
          they do not change an AC sweep);
  LDO     required regulator.vref and regulator.dropout; optional IQ;
          Vout = min(Vin - dropout, regulation), adjustable (FB pin) or
          fixed (`fixed=True`, Vout = vref);
  vref    required regulator.vref; optional Z_out.

Model shapes follow kicad-happy 3.0.1 (MIT, © 2025 Andrew Klofas):
spice/scripts/spice_model_generator.py:generate_opamp_model /
generate_ldo_model / generate_vref_model.
"""

from __future__ import annotations

import math
import re

from kicad_claude.adapters import datasheet_specs as dss

AOL_KEYS = ("Aol", "AOL", "AVOL", "A_OL", "A_VOL", "AVD")


def _name(prefix: str, mpn: str) -> str:
    return f"{prefix}_{re.sub(r'[^A-Za-z0-9_]', '_', mpn.strip())}"


def _sv(rec: dict, group: str, *keys: str):
    grp = (rec or {}).get(group) or {}
    for k in keys:
        if grp.get(k) and dss.value(grp[k]) is not None:
            return k, grp[k]
    return None, None


def _label(rec: dict, sv: dict) -> str:
    return f"behavioural, from datasheet {rec.get('mpn')} p.{sv.get('page')}"


def opamp(rec: dict) -> dict:
    """{"text", "subckt", "pins", "source"} or {"missing": [keys]}. Nodes
    in order +IN -IN OUT V+ V-."""
    _, gbw = _sv(rec, "electrical", "GBW", "GBWP")
    ak, aol = _sv(rec, "electrical", *AOL_KEYS)
    missing = ([] if gbw else ["electrical.GBW"]) + ([] if aol else ["electrical.Aol (dB or V/V)"])
    if missing:
        return {"missing": missing}
    g = dss.value(gbw, "typ")
    a = dss.value(aol, "typ")
    a_lin = 10 ** (a / 20) if aol.get("unit") == "dB" else a
    if not g or not a_lin or a_lin <= 1:
        return {"missing": ["usable GBW / Aol values"]}
    _, vos = _sv(rec, "electrical", "Vos", "VOS", "V_OS")
    _, rin = _sv(rec, "electrical", "R_in", "RIN", "R_IN", "Rin")
    f_pole = g / a_lin
    c_pole = 1.0 / (2 * math.pi * f_pole)          # with R1 = 1 Ohm
    vos_v = abs(dss.value(vos, "typ") or 0.0) if vos else 0.0
    name = _name("OPAMP", rec.get("mpn", "X"))
    text = "\n".join([
        f".subckt {name} inp inn out vcc vee",
        f"* {_label(rec, gbw)}: GBW {g:.4g} Hz, Aol {a_lin:.4g} V/V ({ak}), pole {f_pole:.4g} Hz",
        f"Rin inp inn {dss.value(rin) if rin else 1e12:.6g}",
        f"Vos inp inp_os DC {vos_v:.6g}",
        f"E1 int 0 inp_os inn {a_lin:.6g}",
        "R1 int out_int 1",
        f"C1 out_int 0 {c_pole:.6g}",
        "Eout out 0 out_int 0 1",
        "Rvcc vcc 0 1e9", "Rvee vee 0 1e9",
        f".ends {name}", ""])
    return {"text": text, "subckt": name, "pins": ["+IN", "-IN", "OUT", "V+", "V-"],
            "source": _label(rec, gbw), "gbw_hz": g, "aol": a_lin}


def ldo(rec: dict, fixed: bool = False) -> dict:
    """Nodes vin vout gnd [fb]."""
    reg = (rec or {}).get("regulator") or {}
    vref, drop = reg.get("vref"), reg.get("dropout")
    missing = ([] if vref else ["regulator.vref"]) + ([] if drop else ["regulator.dropout"])
    if missing:
        return {"missing": missing}
    v, d = dss.value(vref, "typ"), dss.value(drop, "max")
    _, iq = _sv(rec, "electrical", "IQ", "I_Q", "Iq", "I_GND")
    riq = 1 / dss.value(iq, "typ") if iq and dss.value(iq, "typ") else 1e12
    name = _name("LDO", rec.get("mpn", "X"))
    target = f"{v:.6g}" if fixed else f"{v:.6g} + ({v:.6g} - V(fb, gnd)) * 1e4"
    text = "\n".join([
        f".subckt {name} vin vout gnd" + ("" if fixed else " fb"),
        f"* {_label(rec, vref)}: Vref {v:g} V, dropout {d:g} V",
        f"Breg ctrl gnd V=min(V(vin, gnd) - {d:.6g}, max(0, {target}))",
        "Rout ctrl vout 0.01",
        f"Riq vin gnd {riq:.6g}",
        *([] if fixed else ["Rfb fb gnd 1e12"]),
        f".ends {name}", ""])
    return {"text": text, "subckt": name, "source": _label(rec, vref),
            "nodes": ["vin", "vout", "gnd"] + ([] if fixed else ["fb"])}


def vref(rec: dict) -> dict:
    """Nodes out gnd."""
    sv = ((rec or {}).get("regulator") or {}).get("vref")
    if not sv:
        return {"missing": ["regulator.vref"]}
    _, z = _sv(rec, "electrical", "Z_out", "ZOUT", "R_out")
    name = _name("VREF", rec.get("mpn", "X"))
    text = "\n".join([
        f".subckt {name} out gnd",
        f"* {_label(rec, sv)}",
        f"Vref out_int gnd DC {dss.value(sv, 'typ'):.6g}",
        f"Rout out_int out {dss.value(z, 'typ') if z else 1e-3:.6g}",
        f".ends {name}", ""])
    return {"text": text, "subckt": name, "source": _label(rec, sv), "nodes": ["out", "gnd"]}
