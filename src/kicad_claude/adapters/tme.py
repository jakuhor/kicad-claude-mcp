"""TME API v2 client — https://api-doc.tme.eu/v2

v2 replaces the old HMAC-SHA1 signed API with OAuth2 client credentials:
POST /auth/token with `Authorization: Basic base64(token:secret)` returns a
bearer token, valid for 5 minutes, that authorises the REST endpoints.
Credentials issued for v2 are rejected by the v1 signed endpoints with
E_AUTHORIZATION_FAILED, so there is no fallback path — this client is v2 only.

Required environment:
    TME_APP_TOKEN
    TME_APP_SECRET
Optional:
    TME_API_BASE   (default: https://api.tme.eu)
    TME_COUNTRY    (default: CZ)
    TME_LANGUAGE   (default: cs)   — sent as Accept-Language
    TME_CURRENCY   (default: CZK)
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger("kicad-claude.adapters.tme")

DEFAULT_BASE = "https://api.tme.eu"
TOKEN_LEEWAY_SEC = 30  # tokens live 300 s; refresh a little early


class TMEError(RuntimeError):
    """API call failed (network, auth, or 4xx/5xx)."""


#: In-process bearer cache: (access_token, expires_at, client_token).
#: Not written to disk — the token is only valid for five minutes.
_token_cache: tuple[str, float, str] | None = None


def _credentials() -> tuple[str, str]:
    token = os.environ.get("TME_APP_TOKEN")
    secret = os.environ.get("TME_APP_SECRET")
    if not token or not secret:
        raise TMEError("TME_APP_TOKEN / TME_APP_SECRET not set; configure them in .env")
    return token, secret


def _base_url() -> str:
    return os.environ.get("TME_API_BASE", DEFAULT_BASE).rstrip("/")


def _country() -> str:
    return os.environ.get("TME_COUNTRY", "CZ")


def _language() -> str:
    return os.environ.get("TME_LANGUAGE", "cs")


def _currency() -> str:
    return os.environ.get("TME_CURRENCY", "CZK")


# --------------------------------------------------------------------------- #
# OAuth2
# --------------------------------------------------------------------------- #


def _fetch_token() -> str:
    token, secret = _credentials()
    basic = base64.b64encode(f"{token}:{secret}".encode()).decode()
    url = f"{_base_url()}/auth/token"
    logger.info("requesting TME bearer token")
    try:
        r = httpx.post(
            url,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"grant_type": "client_credentials"},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        raise TMEError(f"network error fetching token: {e}") from e

    if r.status_code != 200:
        raise TMEError(f"OAuth token request failed: {r.status_code} {r.text[:200]}")

    payload = r.json()
    access = payload.get("access_token")
    if not access:
        raise TMEError(f"OAuth token response had no access_token: {r.text[:200]}")

    global _token_cache
    expires_at = time.time() + int(payload.get("expires_in", 300))
    _token_cache = (access, expires_at, token)
    return access


def get_access_token() -> str:
    """Return a cached bearer token, fetching a new one when it is stale."""
    token, _ = _credentials()
    if _token_cache is not None:
        access, expires_at, cached_for = _token_cache
        if cached_for == token and expires_at > time.time() + TOKEN_LEEWAY_SEC:
            return access
    return _fetch_token()


def _get(path: str, params: dict[str, Any]) -> dict:
    """Authenticated GET. Refreshes the bearer token once on 401."""
    url = f"{_base_url()}{path}"
    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "Accept": "application/json",
        "Accept-Language": _language(),
    }
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=20.0)
        if r.status_code == 401:
            # Token expired mid-flight; one retry with a fresh one.
            logger.info("TME 401 — refreshing bearer token and retrying")
            headers["Authorization"] = f"Bearer {_fetch_token()}"
            r = httpx.get(url, params=params, headers=headers, timeout=20.0)
    except httpx.HTTPError as e:
        raise TMEError(f"network error: {e}") from e

    if r.status_code == 403:
        raise TMEError(
            f"TME denied GET {path} (403). The application is authenticated but "
            "not permitted for this endpoint — check that it is approved and "
            "active at https://developers.tme.eu."
        )
    if r.status_code >= 400:
        raise TMEError(f"GET {path} → {r.status_code}: {r.text[:300]}")

    payload = r.json()
    if payload.get("status") != "OK":
        raise TMEError(f"TME API error: {r.text[:300]}")
    return payload.get("data") or {}


# --------------------------------------------------------------------------- #
# Public methods
# --------------------------------------------------------------------------- #


def search_part(mpn: str, limit: int = 5) -> list[dict]:
    """Look up by manufacturer part number via `GET /products?mpns[]=`."""
    data = _get("/products", {"mpns[]": mpn, "country": _country()})
    return _with_prices((data.get("elements") or [])[:limit])


def search_keyword(query: str, limit: int = 5) -> list[dict]:
    """Free-text search via `GET /products/search`."""
    data = _get(
        "/products/search",
        {
            "phrase": query,
            "scope[]": "products",
            "country": _country(),
            "limit": limit,
        },
    )
    elements = ((data.get("products") or {}).get("elements")) or []
    return _with_prices(elements[:limit])


def _with_prices(products: list[dict]) -> list[dict]:
    """Attach stock and price breaks, which live on a separate endpoint."""
    symbols = [p.get("symbol", "") for p in products if p.get("symbol")]
    by_symbol = _prices_and_stocks(symbols)
    return [_summarize_part(p, by_symbol.get(p.get("symbol", ""), {})) for p in products]


def _prices_and_stocks(symbols: list[str]) -> dict[str, dict]:
    """Fetch stock + price breaks for TME symbols. Keyed by symbol."""
    if not symbols:
        return {}
    try:
        data = _get(
            "/products/data",
            {
                "symbols[]": symbols,
                "scope[]": ["prices", "stock"],
                "country": _country(),
                "currency": _currency(),
            },
        )
    except TMEError as e:
        # Pricing is optional detail — a catalogue hit is still useful.
        logger.info("TME price lookup failed: %s", e)
        return {}
    return {p.get("symbol", ""): p for p in (data.get("elements") or [])}


# --------------------------------------------------------------------------- #
# Response shaping
# --------------------------------------------------------------------------- #


def _summarize_part(p: dict, price: dict) -> dict:
    prices = price.get("prices") or {}
    breaks = prices.get("elements") or []
    unit_price = None
    if breaks:
        # `elements` is ordered by break quantity; the first is the 1-off price.
        try:
            unit_price = float(breaks[0].get("price"))
        except (TypeError, ValueError):
            unit_price = None

    mfr_symbols = p.get("manufacturer_symbols") or []
    symbol = p.get("symbol", "")

    return {
        "source": "tme",
        # `symbol` is TME's own order code; the MPN is the manufacturer symbol.
        "mpn": mfr_symbols[0] if mfr_symbols else symbol,
        "manufacturer": (p.get("manufacturer") or {}).get("name", ""),
        "description": p.get("description", ""),
        "stock": int(price.get("stock_quantity") or 0),
        "unit_price": unit_price,
        "currency": prices.get("currency") or _currency(),
        "datasheet_url": "",
        # v2 returns no product page URL; this is TME's documented URL shape.
        "product_url": (
            f"https://www.tme.eu/{_language()}/details/{symbol.lower()}/"
            if symbol
            else ""
        ),
        "tme_symbol": symbol,
        # GROSS prices include VAT at `vat_rate`; NET ones do not.
        "price_type": prices.get("type", ""),
        "vat_rate": (prices.get("tax") or {}).get("rate"),
        "product_status": p.get("product_status") or [],
    }
