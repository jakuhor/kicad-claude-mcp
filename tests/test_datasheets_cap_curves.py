"""Vendor capacitor curves (OP-52): fetchers against saved vendor responses,
the effective-capacitance helper, the merge into spec records, and the
consumers (PT-COUT-EFF, TH-MLCC-HOT).

- @pytest.mark.network: one live MPN per vendor.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kicad_claude.adapters import board_thermal as bt
from kicad_claude.adapters import cap_curves as cc
from kicad_claude.adapters import datasheet_specs as dss
from kicad_claude.adapters import power_review as pr

FIX = Path(__file__).parent / "fixtures" / "cap_curves"


def _json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, data=None, text=None, status=200):
        self._data, self.status_code = data, status
        self.text = text if text is not None else json.dumps(data)

    def json(self):
        return self._data if self._data is not None else json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=None, response=None)


class FakeHttp:
    """Answers like the vendor back ends; records what was sent."""

    def __init__(self):
        self.sent = []

    def get(self, url, params=None, **kw):
        self.sent.append((url, params))
        if url == cc.MURATA_URL:
            if params["ReqType"] == "GetProductInfo":
                pn = params["partnumbers"]
                ok = pn.startswith("GRM21BR61E106KA73")
                return _Resp([{"nopackingcode": "GRM21BR61E106KA73" if ok else "",
                               "partnumber": pn}])
            req = json.loads(params["ReqChara"])[0]
            tc = req["parameter"].get("tc")
            if req["chara_type"] == "c_temp_capchange":
                return _Resp(_json("murata_temp.json"))
            if tc is None:
                return _Resp(_json("murata_dcbias_25.json"))
            if tc == "85":
                return _Resp(_json("murata_dcbias_85.json"))
            return _Resp({"JsonCharaData": [{"charadata": [
                {"error": "Specified Temp. is out of range", "data": []}]}]})
        if url == cc.SAMSUNG_URL:
            if params["partNumber"] == "CL21A106KAYNNN":
                return _Resp(text=(FIX / "samsung_CL21A106KAYNNN.html").read_text(
                    encoding="utf-8"))
            return _Resp(text="error", status=500)
        raise AssertionError(url)

    def post(self, url, json=None, **kw):
        self.sent.append((url, json))
        if url.endswith("/pn"):
            return _Resp(_json("kemet_pn.json") if json["partNumber"].startswith("C0805C")
                         else "No match found.")
        plot = json["plotType"]
        return _Resp(_json("kemet_vbias.json" if plot == "Vbias" else self.tcc))

    tcc = "kemet_tcc_zero.json"


def test_vendor_routing():
    assert cc.vendor_for("GRM21BR61E106KA73L") == "murata"
    assert cc.vendor_for("CL21A106KAYNNNE") == "samsung"
    assert cc.vendor_for("C0805C106K4PACTU") == "kemet"
    assert cc.vendor_for("C2012X5R1E106K125AB") is None           # TDK
    assert cc.vendor_for("X", "Murata Electronics") == "murata"    # field wins
    assert cc.vendor_for("GRM21BR61E106KA73", "TDK") is None       # named other maker
    assert cc.fetch("C2012X5R1E106K125AB", http=FakeHttp())["status"] == "unsupported"


def test_murata_curves_are_retained_percent_with_temperatures():
    h = FakeHttp()
    r = cc.fetch("GRM21BR61E106KA73L", temps_c=(85, 125), http=h)
    assert r["status"] == "fetched" and r["vendor_pn"] == "GRM21BR61E106KA73"
    cv = r["curves"]
    assert cv["C_bias"]["points"][0] == [0.0, 100.0]               # change 0 % -> 100 % kept
    assert cv["C_bias"]["x_unit"] == "V" and cv["C_bias"]["method"] == "measured"
    assert cv["C_temp"]["x_unit"] == "C" and dss.curve_at(cv["C_temp"], 25) == pytest.approx(
        100, abs=1)
    assert list(cv["C_bias_at"]) == ["85"]                         # 125: out of range
    assert len(cv["C_bias"]["points"]) <= cc.POINTS
    # only the MPN is sent
    assert all(set(p) <= {"callback", "WorkInfo", "ReqType", "partnumbers", "ReqChara"}
               for _, p in h.sent)
    assert cc.fetch("GRM188R61A106KE69", http=h)["status"] == "not_found"


def test_samsung_drops_the_packing_letter():
    r = cc.fetch("CL21A106KAYNNNE", http=FakeHttp())
    assert r["status"] == "fetched" and r["vendor_pn"] == "CL21A106KAYNNN"
    cb, ct = r["curves"]["C_bias"], r["curves"]["C_temp"]
    assert cb["points"][0] == [0.0, 100.0] and cb["points"][-1][1] == pytest.approx(7.29, abs=0.5)
    assert ct["points"][0][0] == -55 and ct["conditions"] == "DC 0 V"   # TCC, not BiasTCC


def test_kemet_model_curves_and_all_zero_tcc():
    h = FakeHttp()
    r = cc.fetch("C0805C106K4PACTU", http=h)
    assert r["status"] == "fetched" and set(r["curves"]) == {"C_bias"}   # zero TCC dropped
    assert r["curves"]["C_bias"]["method"] == "model"
    h.tcc = "kemet_tcc_x7r.json"
    assert "C_temp" in cc.fetch("C0805C106K4PACTU", http=h)["curves"]
    assert cc.fetch("C0805C999K4PACTU", "KEMET", http=h)["status"] == "fetched"
    h2 = FakeHttp()
    h2.post = lambda url, json=None, **kw: _Resp("No match found.")
    assert cc.fetch("C0805C106K4PACTU", http=h2)["status"] == "not_found"


def test_changed_format_is_an_error_not_a_guess():
    h = FakeHttp()
    h.get = lambda url, params=None, **kw: _Resp({"unexpected": 1})
    r = cc.fetch("GRM21BR61E106KA73", http=h)
    assert r["status"] == "error" and r["error"].startswith("format changed")


def _rec():
    return {"curves": {
        "C_bias": {"x_unit": "V", "y_unit": "%", "points": [[0, 100], [10, 40]]},
        "C_temp": {"x_unit": "C", "y_unit": "%", "points": [[-55, 90], [25, 100], [125, 80]]},
        "C_bias_at": {"85": {"x_unit": "V", "y_unit": "%", "points": [[0, 92], [10, 20]]}}}}


def test_c_eff_methods():
    rec = _rec()
    assert dss.c_eff(rec, 5, 25) == {"ratio": pytest.approx(0.7), "method": "bias"}
    mid = dss.c_eff(rec, 10, 55)                                    # between 25 and 85
    assert mid["method"] == "bias_at_temp" and mid["ratio"] == pytest.approx(0.30)
    hot = dss.c_eff(rec, 10, 125)                                   # beyond 85: approx
    assert hot["method"] == "bias_x_temp" and hot["ratio"] == pytest.approx(0.4 * 0.8)
    del rec["curves"]["C_temp"], rec["curves"]["C_bias_at"]
    assert dss.c_eff(rec, 10, 125)["method"] == "bias_only"
    assert dss.c_eff({}, 10, 25) is None


def test_merge_and_cache(tmp_path):
    fetched = cc.fetch("GRM21BR61E106KA73", http=FakeHttp())
    cc.save(tmp_path, fetched)
    back = cc.load(tmp_path, "GRM21BR61E106KA73")
    assert back["curves"] == fetched["curves"] and back["fetched"]
    only = cc.merge(None, back)
    assert only["category"] == "capacitor" and only["curves_only"]
    spec = {"mpn": "GRM21BR61E106KA73", "category": "capacitor",
            "curves": {"C_bias": {"points": [[0, 100], [1, 99]]}, "Z": {"points": []}},
            "absolute_max": {"V": {"max": 25}}}
    m = cc.merge(spec, back)
    assert m["curves"]["C_bias"] == back["curves"]["C_bias"]       # vendor wins
    assert "Z" in m["curves"] and m["absolute_max"] == spec["absolute_max"]
    assert cc.merge(spec, None) is spec


def test_cout_eff_finding(tmp_path):
    from tests.test_review_power_and_connectivity import _netlist

    nl = pr.parse_netlist(_netlist(tmp_path))                     # U1 4.8 V out, C2 22u
    ds = {"U1": {"limits": {"cout_min": (15e-6, None, None, "datasheet BUCK1 p.9")}}}
    derated = {"C2": {"curves": {"C_bias": {"x_unit": "V", "y_unit": "%",
                                            "points": [[0, 100], [4.8, 50], [10, 30]]}}}}

    def rules(specs):
        return {f["rule"]: f for f in pr.analyze(nl, vref={"U1": 0.8}, datasheet=ds,
                                                 specs=specs)["findings"] if f["ref"] == "U1"}
    f = rules(derated)["PT-COUT-EFF"]                               # 22u x 50 % = 11u < 15u
    assert f["c_eff_f"] == pytest.approx(11e-6) and "nominal" not in f["message"]
    assert "PT-COUT-EFF" not in rules({})                           # 22u nominal >= 15u


def test_mlcc_hot_uses_the_temperature_curve(tmp_path):
    from tests.test_emc_si_board_thermal import RATINGS, _board, _cap

    tree = _board(tmp_path, 0, _cap("C1", 25, 20))
    nl = SimpleNamespace(components={
        "U1": {"value": "BUCK", "fields": {}},
        "C1": {"value": "10u X7R", "fields": {}, "description": "", "footprint": "",
               "lib_id": "Device:C"}})
    specs = {"U1": {"mpn": "BUCK1", "thermal": {"psi_jb": {"typ": 5, "unit": "C/W", "page": 2}}},
             "C1": _rec()}
    r = bt.analyze(tree, nl, specs, RATINGS, 25.0, 25.0)
    f = next(f for f in r["findings"] if f["rule"] == "TH-MLCC-HOT")
    assert "temperature curve gives" in f["message"]
    row = next(n for n in r["hot_spots"] if n["cap"] == "C1")
    t = 25.0 + row["board_rise_k"]
    assert row["c_temp_pct"] == pytest.approx(dss.curve_at(_rec()["curves"]["C_temp"], t), abs=0.1)


@pytest.mark.network
@pytest.mark.parametrize("mpn, vendor", [("GRM21BR61E106KA73L", "murata"),
                                         ("CL21A106KAYNNNE", "samsung"),
                                         ("C0805C104K5RACTU", "kemet")])
def test_live_vendor(mpn, vendor):
    r = cc.fetch(mpn, temps_c=(85,))
    assert r["status"] == "fetched" and r["vendor"] == vendor, r
    assert r["curves"]["C_bias"]["points"][0][1] == pytest.approx(100, abs=2)


def test_fetch_cap_curves_tool_caches_and_merges(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    from kicad_claude.tools import common, datasheets
    from tests.test_datasheets import _comp, _netlist

    fake = FakeHttp()
    monkeypatch.setattr(cc, "_Http", lambda: fake)
    nl_path = _netlist(tmp_path, [
        _comp("C1", "10u", "GRM21BR61E106KA73L"), _comp("C2", "10u", "GRM21BR61E106KA73L"),
        _comp("C3", "10u", "C2012X5R1E106K125AB"), _comp("C4", "100n"),   # TDK; no MPN
        _comp("U1", "MCU", "STM32")])                                     # not a capacitor
    ds = tmp_path / "datasheets"
    monkeypatch.setattr(datasheets, "project_netlist", lambda p: nl_path)
    monkeypatch.setattr(datasheets, "_ds_dir", lambda d: ds)
    mcp = FastMCP("t")
    datasheets.register(mcp)
    tool = mcp._tool_manager.get_tool("fetch_cap_curves").fn
    r = tool(temps_c=[85])
    assert r["counts"] == {"fetched": 1, "unsupported": 1}
    row = r["parts"]["GRM21BR61E106KA73L"]
    assert row["refs"] == ["C1", "C2"] and row["vendor"] == "murata"
    assert "C_bias_at ['85'] degC" in row["curves"] and row["retained_pct"]
    n = len(fake.sent)
    again = tool(parts=["C1"])
    assert again["parts"]["GRM21BR61E106KA73L"]["status"] == "cached" and len(fake.sent) == n
    # specs_by_ref merges the cached curves into a curves-only record
    monkeypatch.setattr(common.state, "get_active_or_none",
                        lambda: SimpleNamespace(path=tmp_path))
    monkeypatch.setattr("kicad_claude.adapters.datasheet_index.datasheets_dir", lambda p: ds)
    nl = pr.parse_netlist(nl_path)
    specs = common.specs_by_ref(nl)
    assert set(specs) == {"C1", "C2"} and "C_bias" in specs["C1"]["curves"]
