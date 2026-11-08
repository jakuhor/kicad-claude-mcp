"""JLCPCB assembly parts library client (the LCSC catalogue JLCPCB places from).

Uses the public parts-search endpoint the jlcpcb.com/parts page calls. No API
key. It covers what the community `jlcsearch` index misses (RF modules, many
ICs and connectors), and is the only source that says
whether a part is a JLCPCB **Basic** part (no feeder-loading fee), a
**Preferred** extended part, or an **Extended** part.

Prices are JLCPCB's assembly prices in USD, per break quantity.

Optional environment:
    JLCPCB_API_BASE  (default: https://jlcpcb.com)

The endpoint is undocumented and may change; failures surface as JLCPCBError
so `check_availability` reports them per source instead of failing the call.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("kicad-claude.adapters.jlcpcb")

DEFAULT_BASE = "https://jlcpcb.com"
_SEARCH_PATH = "/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"


class JLCPCBError(RuntimeError):
    """API call failed (network, HTTP error, or unexpected payload)."""


def _base_url() -> str:
    return os.environ.get("JLCPCB_API_BASE", DEFAULT_BASE).rstrip("/")


def _post(keyword: str, limit: int) -> list[dict]:
    body = {"keyword": keyword, "currentPage": 1, "pageSize": max(1, min(limit, 50))}
    try:
        r = httpx.post(
            _base_url() + _SEARCH_PATH,
            json=body,
            headers={"User-Agent": "kicad-claude-mcp", "Accept": "application/json"},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        raise JLCPCBError(f"JLCPCB request failed: {e}") from e
    if r.status_code != 200:
        raise JLCPCBError(f"JLCPCB HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as e:
        raise JLCPCBError(f"JLCPCB returned non-JSON: {r.text[:200]}") from e
    if data.get("code") not in (200, "200", None):
        raise JLCPCBError(f"JLCPCB error {data.get('code')}: {data.get('message')}")
    page = ((data.get("data") or {}).get("componentPageInfo") or {})
    return list(page.get("list") or [])


def library_type(c: dict) -> str:
    """`basic`, `preferred` (extended, no loading fee) or `extended`."""
    if (c.get("componentLibraryType") or "").lower() == "base":
        return "basic"
    if c.get("preferredComponentFlag"):
        return "preferred"
    return "extended"


def _price_breaks(c: dict) -> list[dict]:
    out = []
    for p in c.get("componentPrices") or []:
        try:
            out.append({"qty": int(p["startNumber"]), "unit_price": float(p["productPrice"])})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda b: b["qty"])


def _summarize_part(c: dict) -> dict[str, Any]:
    breaks = _price_breaks(c)
    suffix = c.get("urlSuffix") or ""
    code = c.get("componentCode") or ""
    return {
        "source": "jlcpcb",
        "mpn": c.get("componentModelEn") or "",
        "manufacturer": c.get("componentBrandEn") or "",
        "description": c.get("describe") or c.get("componentNameEn") or "",
        "package": c.get("componentSpecificationEn") or "",
        "stock": int(c.get("stockCount") or 0),
        "unit_price": breaks[0]["unit_price"] if breaks else None,
        "currency": "USD",
        "price_breaks": breaks,
        "datasheet_url": c.get("dataManualUrl") or c.get("dataManualOfficialLink") or "",
        "product_url": f"https://jlcpcb.com/partdetail/{suffix}" if suffix else "",
        "lcsc_url": c.get("lcscGoodsUrl") or "",
        "sku": code,
        "lcsc": code,
        "library_type": library_type(c),
    }


def search_part(query: str, limit: int = 5) -> list[dict]:
    """Search by MPN or LCSC number (`C51118`). Exact MPN / LCSC matches first."""
    q = query.strip()
    rows = [_summarize_part(c) for c in _post(q, limit)]
    qu = q.upper()
    rows.sort(key=lambda r: (r["mpn"].upper() != qu and r["lcsc"].upper() != qu))
    return rows[:limit]


def search_keyword(query: str, limit: int = 5) -> list[dict]:
    return search_part(query, limit)
