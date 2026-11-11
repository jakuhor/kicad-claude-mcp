"""Download a datasheet PDF from the first URL that really serves one.

Manufacturer and distributor sites fail in different ways:
an expired TLS certificate (onsemi), bot-protection HTML instead of the PDF
(Littelfuse), automated downloads blocked (Mouser). So each candidate URL is
tried in turn and a response is accepted only when its body starts with
`%PDF`. TLS verification is **never** disabled: a certificate error is
reported as that candidate's failure and the next one is tried.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx

MAX_BYTES = 60 * 1024 * 1024
_UA = "Mozilla/5.0 (compatible; kicad-claude-mcp datasheet fetch)"


def _filename_for(url: str, mpn: str) -> str:
    """The vendor's own file name when the URL ends in .pdf, else `<MPN>.pdf`."""
    name = unquote(Path(urlparse(url).path).name)
    if not name.lower().endswith(".pdf"):
        name = f"{mpn}.pdf"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


MAX_REDIRECTS = 5
TOTAL_TIMEOUT_S = 120.0
# Set to 1 to download from private / loopback hosts (an intranet datasheet
# server). Off by default: a Datasheet URL comes from whatever schematic was
# opened, and must not make this machine reach its own network.
ALLOW_PRIVATE_ENV = "KICAD_MCP_ALLOW_PRIVATE_DOWNLOADS"


def _host_addresses(host: str) -> list[str]:
    """Every address `host` resolves to."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def check_url(url: str) -> str:
    """Why `url` may not be fetched, or "" when it may.

    http(s) only; no credentials, backslash, blank, `%` or control character in
    the host part (parsers disagree on such hosts); and every address the host
    resolves to must be public — not loopback, private, link-local, multicast
    or reserved. Checked again on every redirect.

    The name is resolved here and again by httpx, so a host whose DNS answer
    changes between the two (rebinding) is not caught; this blocks plain
    private-address URLs and redirects, not a hostile DNS server.
    """
    try:
        p = urlparse(url)
    except ValueError as exc:
        return f"malformed URL: {exc}"
    if p.scheme not in ("http", "https"):
        return f"not an http(s) URL: {url[:120]!r}"
    if "\\" in url or re.search(r"[\s%\x00-\x1f\x7f]", p.netloc):
        return "malformed host"
    if "@" in p.netloc:
        return "URL carries credentials"
    host = p.hostname
    if not host:
        return "URL has no host"
    if os.environ.get(ALLOW_PRIVATE_ENV, "").strip() not in ("", "0"):
        return ""
    try:
        addrs = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addrs = _host_addresses(host)
        except (OSError, UnicodeError) as exc:
            return f"host {host!r} does not resolve ({exc})"
    for a in addrs:
        if not ipaddress.ip_address(a.split("%")[0]).is_global:
            return (f"host {host!r} is a non-public address ({a}); set "
                    f"{ALLOW_PRIVATE_ENV}=1 to allow intranet downloads")
    return ""


def try_download(url: str, timeout: float = 30.0,
                 total_timeout: float = TOTAL_TIMEOUT_S) -> tuple[bytes | None, str]:
    """GET `url`; return (pdf_bytes, "") or (None, reason).

    Redirects are followed by hand, at most `MAX_REDIRECTS`, each target
    passing `check_url`. The whole download, headers included, stops after
    `total_timeout` seconds and at `MAX_BYTES`.
    """
    if url.startswith("//"):
        url = "https:" + url
    deadline = time.monotonic() + total_timeout
    buf = bytearray()
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            reason = check_url(url)
            if reason:
                return None, reason
            left = deadline - time.monotonic()
            if left <= 0:
                return None, f"took longer than {total_timeout:.0f} s"
            with httpx.stream(
                "GET", url, follow_redirects=False, timeout=min(timeout, left),
                headers={"User-Agent": _UA, "Accept": "application/pdf,*/*;q=0.8"},
            ) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200:
                    return None, f"HTTP {r.status_code}"
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_BYTES:
                        return None, f"larger than {MAX_BYTES // (1024 * 1024)} MB"
                    if time.monotonic() > deadline:
                        return None, f"took longer than {total_timeout:.0f} s"
                    if len(buf) >= 1024 and not buf.lstrip()[:5] == b"%PDF-":
                        break
                break
        else:
            return None, f"more than {MAX_REDIRECTS} redirects"
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
