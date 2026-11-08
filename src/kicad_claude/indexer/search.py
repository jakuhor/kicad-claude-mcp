"""Fuzzy search across the indexed library entries.

Uses `rapidfuzz` for ranking. The "haystack" for each entry is a single
string built from `lib_id + description + keywords/tags` so a query like
"esp32-s3" matches both library-id substrings and keyword text.
"""

from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz, process, utils


def _symbol_haystack(entry: dict[str, Any]) -> str:
    return " ".join(
        filter(
            None,
            (
                entry.get("lib_id", ""),
                entry.get("description", ""),
                entry.get("keywords", ""),
            ),
        )
    )


def _footprint_haystack(entry: dict[str, Any]) -> str:
    return " ".join(
        filter(
            None,
            (
                entry.get("lib_id", ""),
                entry.get("description", ""),
                entry.get("tags", ""),
            ),
        )
    )


def _search(
    query: str,
    entries: dict[str, dict[str, Any]],
    haystack_fn,
    max_results: int,
    score_cutoff: int,
) -> list[dict[str, Any]]:
    if not query.strip() or not entries:
        return []

    # Name matches first (open_points_4 R4). WRatio over lib_id + description
    # + keywords rewards partial token overlap, so `ESP32-C6-MINI-1` lost to
    # 4000-series logic. Tiers on the normalized symbol/footprint name (the
    # part after `Lib:`): exact 100, prefix 99, substring 98; shorter names
    # first within a tier. Fuzzy results fill the rest, capped at 97.
    q = utils.default_process(query)
    tiers: list[tuple[int, int, str]] = []  # (-score, name length, lib_id)
    if q:
        for lid in entries:
            name = utils.default_process(lid.split(":", 1)[-1])
            if name == q:
                tiers.append((-100, len(name), lid))
            elif name.startswith(q):
                tiers.append((-99, len(name), lid))
            elif len(q) >= 3 and q in name:
                tiers.append((-98, len(name), lid))
    tiers.sort()
    out: list[dict[str, Any]] = []
    for neg, _len, lid in tiers[:max_results]:
        entry = dict(entries[lid])
        entry["_score"] = float(-neg)
        out.append(entry)
    if len(out) >= max_results:
        return out
    seen = {t[2] for t in tiers[:max_results]}

    # Build (lib_id, haystack) parallel lists once.
    lib_ids = list(entries.keys())
    haystacks = [haystack_fn(entries[k]) for k in lib_ids]

    # default_process lowercases and strips non-alphanumeric, so "ESP32-S3"
    # and "esp32 s3" normalize to the same form before scoring.
    results = process.extract(
        query,
        haystacks,
        scorer=fuzz.WRatio,
        processor=utils.default_process,
        limit=max_results + len(seen),
        score_cutoff=score_cutoff,
    )
    for _haystack, score, idx in results:
        if lib_ids[idx] in seen:
            continue
        entry = dict(entries[lib_ids[idx]])
        entry["_score"] = round(min(score, 97.0), 1)
        out.append(entry)
        if len(out) >= max_results:
            break
    return out


def search_symbols(
    query: str,
    index: dict[str, Any],
    max_results: int = 10,
    score_cutoff: int = 50,
) -> list[dict[str, Any]]:
    return _search(
        query,
        index.get("symbols", {}),
        _symbol_haystack,
        max_results,
        score_cutoff,
    )


def search_footprints(
    query: str,
    index: dict[str, Any],
    max_results: int = 10,
    score_cutoff: int = 50,
) -> list[dict[str, Any]]:
    return _search(
        query,
        index.get("footprints", {}),
        _footprint_haystack,
        max_results,
        score_cutoff,
    )
