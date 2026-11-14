"""Project datasheet directory: manifest, PDF text, MPN -> PDF matching.

`<project>/datasheets/manifest.kicad-claude.json` maps each MPN in the
schematic to its PDF in `<project>/datasheets/`:

    {"version": 1,
     "parts": {"LT8471IUFD#TRPBF": {"file": "8471fd.pdf", "sha256": "...",
                                    "how": "datasheet_field", "refs": ["U2"],
                                    "value": "...", "manufacturer": "...",
                                    "url": "...", "in_schematic": true,
                                    "updated": "2026-11-08T..."}}}

The name keeps clear of kicad-happy's own `manifest.json`.

Vendor file names often do not contain the MPN (`8471fd.pdf`,
`TVS-SMF-1103.pdf`), so an existing PDF is matched to an MPN, in order of
trust: the manifest, an explicit assignment, the base name of the symbol's
Datasheet URL, then the PDF's own text (the MPN, or its longest prefix down to
the family stem `LT8471`, appears in it). Text comes from `pypdf` and is
cached per PDF hash under the user cache dir.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from kicad_claude.utils.kicad_paths import cache_dir

MANIFEST_NAME = "manifest.kicad-claude.json"
MAX_TEXT_PAGES = 80

logging.getLogger("pypdf").setLevel(logging.ERROR)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- manifest -------------------------------------------------------------- #


def datasheets_dir(project_dir: Path) -> Path:
    """`<project>/datasheets` if it exists, else the nearest `datasheets/` in
    a parent folder up to the git root (multi-board repos keep one shared
    folder, e.g. firbox/datasheets for hardware/base_board). Defaults to
    `<project>/datasheets` when none exists."""
    for d in [project_dir, *project_dir.parents]:
        if (d / "datasheets").is_dir():
            return d / "datasheets"
        if (d / ".git").exists():
            break
    return project_dir / "datasheets"


def manifest_path(ds_dir: Path) -> Path:
    return ds_dir / MANIFEST_NAME


def load_manifest(ds_dir: Path) -> dict:
    p = manifest_path(ds_dir)
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("parts", {})
        return data
    return {"version": 1, "parts": {}}


def save_manifest(ds_dir: Path, data: dict) -> Path:
    ds_dir.mkdir(parents=True, exist_ok=True)
    data["parts"] = dict(sorted(data["parts"].items()))
    p = manifest_path(ds_dir)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def pdf_files(ds_dir: Path) -> list[Path]:
    """Top-level PDFs only; sub-folders (ingest/, input/) are not active parts."""
    if not ds_dir.is_dir():
        return []
    return sorted(p for p in ds_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")


# ---- PDF text -------------------------------------------------------------- #


def pdf_pages(path: Path, digest: str | None = None) -> list[str]:
    """Text of each page (up to MAX_TEXT_PAGES), cached by file hash."""
    digest = digest or sha256(path)
    cache = cache_dir() / "pdf_text" / f"{digest}.json"
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))["pages"]
        except (ValueError, KeyError):
            pass
    import pypdf

    pages: list[str] = []
    try:
        reader = pypdf.PdfReader(str(path))
        for page in reader.pages[:MAX_TEXT_PAGES]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page must not stop the rest
                pages.append("")
    except Exception as e:  # noqa: BLE001 - corrupt / encrypted PDF
        logging.getLogger("kicad-claude.datasheets").warning("pdf text %s: %s", path, e)
        return []
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"file": path.name, "pages": pages}), encoding="utf-8")
    return pages


# ---- matching ------------------------------------------------------------- #


def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def stem(mpn: str) -> str:
    """Family stem: leading letters + digits (+ one letter + digits):
    `LT8471IUFD#TRPBF` -> LT8471, `TPS7A4701MRGWREP` -> TPS7A4701. Falls back
    to the whole normalized MPN when it does not start with letters."""
    n = norm(mpn)
    m = re.match(r"[A-Z]+\d+(?:[A-Z]\d+)?", n)
    s = m.group(0) if m else n
    return s if len(s) >= 5 else n


def url_basename(url: str) -> str:
    if not url or not url.lower().startswith(("http://", "https://", "//")):
        return ""
    return unquote(Path(urlparse(url).path).name)


def content_score(mpn: str, text_norm: str) -> int:
    """Length of the longest prefix of the normalized MPN found in the text,
    not shorter than its stem; 0 when even the stem is absent."""
    n, s = norm(mpn), stem(mpn)
    for k in range(len(n), len(s) - 1, -1):
        if n[:k] in text_norm:
            return k
    return 0


def match_existing(parts: dict[str, dict], files: list[Path],
                   digests: dict[str, str]) -> dict[str, dict]:
    """For each MPN without a manifest entry, the best existing PDF:
    {mpn: {file, how, score, alternatives}}. `parts[mpn]["datasheet"]` is the
    symbol's Datasheet field."""
    by_name = {f.name.lower(): f for f in files}
    texts: dict[str, str] = {}

    def text(f: Path) -> str:
        if f.name not in texts:
            texts[f.name] = norm(" ".join(pdf_pages(f, digests.get(f.name))))
        return texts[f.name]

    out: dict[str, dict] = {}
    for mpn, info in parts.items():
        base = url_basename(info.get("datasheet", "")).lower()
        if base and base in by_name:
            out[mpn] = {"file": by_name[base].name, "how": "datasheet_field", "score": None,
                        "alternatives": []}
            continue
        scored = []
        for f in files:
            sc = content_score(mpn, text(f))
            if sc:
                bonus = 1 if stem(mpn) in norm(f.stem) else 0
                scored.append((sc + bonus, "content", f.name))
        if not scored:
            # Scanned / image-only PDFs have no text: fall back to a file
            # name that is the MPN or a prefix of it down to the stem.
            n = norm(mpn)
            for f in files:
                fs = norm(f.stem)
                if len(fs) >= len(stem(mpn)) and n.startswith(fs):
                    scored.append((len(fs), "filename", f.name))
        if not scored:
            continue
        scored.sort(reverse=True)
        best = scored[0]
        out[mpn] = {"file": best[2], "how": best[1], "score": best[0],
                    "alternatives": [n for s, _, n in scored[1:] if s == best[0]]}
    return out
