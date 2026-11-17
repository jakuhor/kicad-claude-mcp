"""Mouser Search API V2 client.

Single-key auth via query string (`apiKey=<KEY>`). No OAuth, no caching needed.

Required environment:
    MOUSER_API_KEY
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any

import httpx

logger = logging.getLogger("kicad-claude.adapters.mouser")

from kicad_claude.utils.logging import quiet_http  # noqa: E402
from kicad_claude.adapters import temp_grade  # noqa: E402

quiet_http()

DEFAULT_BASE = "https://api.mouser.com/api/v2"

#: Mouser's search API allows about 30 calls per minute per key.
RATE_LIMIT = 30
RATE_WINDOW_S = 60.0
_calls: deque[float] = deque()


class MouserError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("MOUSER_API_KEY")
    if not key:
        raise MouserError("MOUSER_API_KEY not set; configure it in .env")
    return key


def _base_url() -> str:
    return os.environ.get("MOUSER_API_BASE", DEFAULT_BASE).rstrip("/")


def _throttle() -> None:
    """Wait so that no more than RATE_LIMIT calls start in any RATE_WINDOW_S."""
    now = time.monotonic()
    while _calls and now - _calls[0] >= RATE_WINDOW_S:
        _calls.popleft()
    if len(_calls) >= RATE_LIMIT:
        wait = RATE_WINDOW_S - (now - _calls[0])
        logger.info("Mouser rate limit: waiting %.1f s", wait)
        time.sleep(wait)
        _calls.popleft()
    _calls.append(time.monotonic())


def _too_many(text: str) -> MouserError:
    return MouserError(
        f"Mouser TooManyRequests: the API allows about {RATE_LIMIT} calls per "
        f"minute (and a daily cap); retry later. {text[:200]}")


def _post(path: str, body: dict[str, Any]) -> Any:
    url = f"{_base_url()}{path}"
    _throttle()
    try:
        r = httpx.post(
            url,
            params={"apiKey": _api_key()},
            json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        raise MouserError(f"network error: {e}") from e
    if r.status_code == 429:
        raise _too_many(r.text)
    if r.status_code >= 400:
        raise MouserError(f"POST {path} → {r.status_code}: {r.text[:300]}")
    payload = r.json()
    errs = payload.get("Errors") or []
    if errs and "toomanyrequests" in str(errs).lower().replace(" ", ""):
        raise _too_many(str(errs))
    if errs:
        # Mouser returns 200 with an Errors array on auth failures.
        raise MouserError(f"Mouser API errors: {errs}")
    return payload


# --------------------------------------------------------------------------- #
# Public methods
# --------------------------------------------------------------------------- #


def search_part(mpn: str) -> list[dict]:
    """Look up by manufacturer part number. Returns up to ~50 simplified results."""
    payload = _post(
        "/search/partnumber",
        {
            "SearchByPartRequest": {
                "mouserPartNumber": mpn,
                "partSearchOptions": "1",  # 1 = "Begins with"; 2 = exact
            }
        },
    )
    parts = (payload.get("SearchResults") or {}).get("Parts") or []
    return [_summarize_part(p) for p in parts]


def search_keyword(query: str, records: int = 10) -> list[dict]:
    """Free-text keyword search."""
    payload = _post(
        "/search/keyword",
        {
            "SearchByKeywordRequest": {
                "keyword": query,
                "records": records,
                "startingRecord": 0,
                "searchOptions": "",
                "searchWithYourSignUpLanguage": "false",
            }
        },
    )
    parts = (payload.get("SearchResults") or {}).get("Parts") or []
    return [_summarize_part(p) for p in parts]


# --------------------------------------------------------------------------- #
# Response shaping
# --------------------------------------------------------------------------- #


def _parse_price(raw: str) -> float | None:
    """Parse a localized Mouser price string: "$1,234.56", "0,420 €", "1 234,50 Kč".

    The currency text depends on the account locale, so keep only digits and
    separators instead of stripping a fixed list of symbols. When both "," and
    "." appear, the last one is the decimal separator; a lone "," is decimal.
    """
    cleaned = "".join(c for c in raw if c.isdigit() or c in ",.").strip(",.")
    if "," in cleaned and "." in cleaned:
        thousands = "," if cleaned.rfind(".") > cleaned.rfind(",") else "."
        cleaned = cleaned.replace(thousands, "")
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _summarize_part(p: dict) -> dict:
    # Mouser stock is a string like "5,432". Try to coerce.
    stock_raw = p.get("AvailabilityInStock") or "0"
    try:
        stock = int(str(stock_raw).replace(",", "").replace(".", ""))
    except ValueError:
        stock = 0

    price_breaks = p.get("PriceBreaks") or []
    unit_price = None
    currency = ""
    if price_breaks:
        first = price_breaks[0]
        unit_price = _parse_price(first.get("Price", "") or "")
        currency = first.get("Currency", "")
    breaks = []
    for b in price_breaks:
        price = _parse_price(b.get("Price", "") or "")
        try:
            breaks.append({"qty": int(b.get("Quantity")), "unit_price": price})
        except (TypeError, ValueError):
            continue

    return {
        "source": "mouser",
        "mpn": p.get("ManufacturerPartNumber", ""),
        "manufacturer": p.get("Manufacturer", ""),
        "description": p.get("Description", ""),
        "stock": stock,
        "unit_price": unit_price,
        "currency": currency,
        "price_breaks": sorted((b for b in breaks if b["unit_price"] is not None),
                               key=lambda b: b["qty"]),
        "sku": p.get("MouserPartNumber", "") or "",
        "min_qty": _int(p.get("Min")),
        "order_multiple": _int(p.get("Mult")),
        "datasheet_url": p.get("DataSheetUrl", "") or "",
        "product_url": p.get("ProductDetailUrl", "") or "",
        "lead_time": p.get("LeadTime", ""),
        "lifecycle": p.get("LifecycleStatus") or "",
        "suggested_replacement": p.get("SuggestedReplacement") or "",
        "operating_temp": temp_grade.attr_text(p.get("ProductAttributes") or [],
                                          "AttributeName", "AttributeValue"),
    }


def _int(raw: Any) -> int | None:
    try:
        return int(str(raw).replace(",", "").replace(".", ""))
    except (TypeError, ValueError):
        return None
