"""Farnell / element14 Product Search API client.

One REST endpoint covers every element14 storefront (Farnell, Newark,
element14 APAC); `storeInfo.id` picks the store and therefore the currency.
Auth is a single API key in the query string — no OAuth.

Required environment:
    FARNELL_API_KEY
Optional:
    FARNELL_API_BASE  (default: https://api.element14.com)
    FARNELL_STORE     (default: cz.farnell.com)
    FARNELL_CURRENCY  (default: CZK)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("kicad-claude.adapters.farnell")

DEFAULT_BASE = "https://api.element14.com"


class FarnellError(RuntimeError):
    """API call failed (network, auth, or 4xx/5xx)."""


def _api_key() -> str:
    key = os.environ.get("FARNELL_API_KEY")
    if not key:
        raise FarnellError("FARNELL_API_KEY not set; configure it in .env")
    return key


def _base_url() -> str:
    return os.environ.get("FARNELL_API_BASE", DEFAULT_BASE).rstrip("/")


def _store() -> str:
    return os.environ.get("FARNELL_STORE", "cz.farnell.com")


def _currency() -> str:
    return os.environ.get("FARNELL_CURRENCY", "CZK")


def _get(term: str, limit: int) -> list[dict]:
    params = {
        "term": term,
        "storeInfo.id": _store(),
        "resultsSettings.offset": 0,
        "resultsSettings.numberOfResults": limit,
        "resultsSettings.responseGroup": "large",
        "callInfo.omitXmlSchema": "false",
        "callInfo.responseDataFormat": "json",
        "callInfo.apiKey": _api_key(),
    }
    url = f"{_base_url()}/catalog/products"
    try:
        r = httpx.get(url, params=params, headers={"Accept": "application/json"}, timeout=20.0)
    except httpx.HTTPError as e:
        raise FarnellError(f"network error: {e}") from e

    if r.status_code >= 400:
        raise FarnellError(f"GET /catalog/products → {r.status_code}: {r.text[:300]}")

    payload = r.json()
    if "Fault" in payload:
        fault = payload["Fault"]
        raise FarnellError(f"element14 fault: {fault}")

    # The result is wrapped in a key that depends on the search type
    # (manufacturerPartNumberSearchReturn, keywordSearchReturn, ...).
    for value in payload.values():
        if isinstance(value, dict) and "products" in value:
            return value.get("products") or []
    return []


# --------------------------------------------------------------------------- #
# Public methods
# --------------------------------------------------------------------------- #


def search_part(mpn: str, limit: int = 5) -> list[dict]:
    """Look up by manufacturer part number."""
    return [_summarize_part(p) for p in _get(f"manuPartNum:{mpn}", limit)]


def search_keyword(query: str, limit: int = 5) -> list[dict]:
    """Free-text keyword search."""
    return [_summarize_part(p) for p in _get(f"any:{query}", limit)]


# --------------------------------------------------------------------------- #
# Response shaping
# --------------------------------------------------------------------------- #


def _first_datasheet(p: dict) -> str:
    for sheet in p.get("datasheets") or []:
        if sheet.get("url"):
            return sheet["url"]
    return ""


def _stock(p: dict) -> int:
    stock: Any = p.get("stock") or {}
    if isinstance(stock, dict):
        raw = stock.get("level", 0)
    else:
        raw = stock
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _summarize_part(p: dict) -> dict:
    prices = p.get("prices") or []
    unit_price = None
    if prices:
        # `prices` is ordered by break quantity; the first is the 1-off price.
        try:
            unit_price = float(prices[0].get("cost"))
        except (TypeError, ValueError):
            unit_price = None

    sku = p.get("sku", "")
    return {
        "source": "farnell",
        "mpn": p.get("translatedManufacturerPartNumber") or p.get("displayName", ""),
        "manufacturer": p.get("brandName") or p.get("vendorName", ""),
        "description": p.get("displayName", ""),
        "stock": _stock(p),
        "unit_price": unit_price,
        "currency": _currency(),
        "datasheet_url": _first_datasheet(p),
        "product_url": f"https://{_store()}/{sku}" if sku else "",
        "sku": sku,
    }
