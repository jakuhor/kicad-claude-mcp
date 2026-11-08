"""Download a datasheet PDF from the first URL that really serves one.

Manufacturer and distributor sites fail in different ways (open_points_4 R6):
an expired TLS certificate (onsemi), bot-protection HTML instead of the PDF
(Littelfuse), automated downloads blocked (Mouser). So each candidate URL is
tried in turn and a response is accepted only when its body starts with
`%PDF`. TLS verification is **never** disabled: a certificate error is
reported as that candidate's failure and the next one is tried.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

MAX_BYTES = 60 * 1024 * 1024
_UA = "Mozilla/5.0 (compatible; kicad-claude-mcp datasheet fetch)"


def _filename_for(url: str, mpn: str) -> str:
    """The vendor's own file name when the URL ends in .pdf, else `<MPN>.pdf`."""
    name = unquote(Path(urlparse(url).path).name)
    if not name.lower().endswith(".pdf"):
        name = f"{mpn}.pdf"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


def try_download(url: str, timeout: float = 30.0) -> tuple[bytes | None, str]:
    """GET `url`; return (pdf_bytes, "") or (None, reason)."""
    if url.startswith("//"):
        url = "https:" + url
    if not url.lower().startswith(("http://", "https://")):
        return None, f"not an http(s) URL: {url!r}"
    try:
        with httpx.stream(
            "GET", url, follow_redirects=True, timeout=timeout,
            headers={"User-Agent": _UA, "Accept": "application/pdf,*/*;q=0.8"},
        ) as r:
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    return None, f"larger than {MAX_BYTES // (1024 * 1024)} MB"
                if len(buf) >= 1024 and not buf.lstrip()[:5] == b"%PDF-":
                    break
    except httpx.ConnectError as e:
        msg = str(e)
        if "CERTIFICATE" in msg.upper() or "SSL" in msg.upper():
            return None, f"TLS certificate error (not bypassed): {msg[:160]}"
        return None, f"connection failed: {msg[:160]}"
    except httpx.HTTPError as e:
        return None, f"request failed: {str(e)[:160]}"
    body = bytes(buf)
    if not body.lstrip()[:5] == b"%PDF-":
        kind = "HTML page (bot protection or landing page)" \
            if b"<html" in body[:2048].lower() else "not a PDF"
        return None, kind
    return body, ""


def fetch_first_pdf(
    candidates: list[dict], mpn: str, dest_dir: Path, overwrite: bool = False
) -> dict:
    """Try `candidates` ([{source, url}]) in order; save the first real PDF.

    Returns `{saved, path, source, url, attempts: [{source, url, ok, reason}]}`.
    An existing file is kept unless `overwrite`.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    attempts: list[dict] = []
    tried: set[str] = set()
    for c in candidates:
        url = (c.get("url") or "").strip()
        if not url or url in tried:
            continue
        tried.add(url)
        target = dest_dir / _filename_for(url, mpn)
        if target.exists() and not overwrite:
            attempts.append({"source": c["source"], "url": url, "ok": True,
                             "reason": "already on disk"})
            return {"saved": False, "path": str(target), "source": c["source"],
                    "url": url, "attempts": attempts, "existing": True}
        body, reason = try_download(url)
        attempts.append({"source": c["source"], "url": url, "ok": body is not None,
                         "reason": reason or "ok"})
        if body is not None:
            target.write_bytes(body)
            return {"saved": True, "path": str(target), "source": c["source"],
                    "url": url, "bytes": len(body), "attempts": attempts}
    return {"saved": False, "path": None, "source": None, "url": None,
            "attempts": attempts}
