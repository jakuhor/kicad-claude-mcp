"""Read and write `.kicad_dru` custom DRC rule files.

The DRU file is plain text, s-expression-flavoured. We keep rule operations
at the textual level — generating fresh content on every write — and use
sexpdata only for reading existing rules so we can list and remove them.

File layout::

    (version 1)

    (rule "Power clearance"
        (constraint clearance (min 0.5mm))
        (condition "A.NetClass == 'Power' || B.NetClass == 'Power'")
        (severity error)
    )

Common constraint types::

    clearance, hole_clearance, silk_clearance, physical_clearance,
    physical_hole_clearance, annular_width,
    track_width, via_diameter, via_drill, hole_size,
    diff_pair_gap, diff_pair_uncoupled, length, skew,
    text_height, text_thickness, disallow

Severities: error | warning | info | ignore
"""

from __future__ import annotations

import logging
from pathlib import Path

import sexpdata

from kicad_claude.adapters.sch_io import is_call

logger = logging.getLogger("kicad-claude.adapters.drc_rules")

VALID_CONSTRAINTS = frozenset({
    "clearance", "hole_clearance", "silk_clearance",
    "physical_clearance", "physical_hole_clearance",
    "annular_width",
    "track_width", "via_diameter", "via_drill",
    "hole_size", "track_segment_length",
    "diff_pair_gap", "diff_pair_uncoupled",
    "length", "skew",
    "text_height", "text_thickness",
    "min_resolved_spokes", "zone_connection",
    "disallow", "courtyard_clearance",
    "edge_clearance",
})

VALID_SEVERITIES = frozenset({"error", "warning", "info", "ignore"})


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


def read_rules(path: Path) -> list[dict]:
    """Parse a `.kicad_dru` file and return a list of rule dicts.

    A missing file is []. A file that does not parse raises `ValueError`.
    The dicts carry name, constraint and min/opt/max, condition, layer and
    severity; other clauses are not listed, but the edits below keep them.
    """
    path = Path(path)
    if not path.is_file():
        return []
    # Raises on a file it cannot read: an empty list here would let the next
    # write replace every rule the user has.
    forms = [form for form, _span in _rule_forms(_read_text(path), path)]

    rules: list[dict] = []
    for form in forms:
        if not is_call(form, "rule"):
            continue
        if len(form) < 2 or not isinstance(form[1], str):
            continue
        name = form[1]
        rule = {"name": name, "constraint_type": "", "severity": "error"}
        for child in form[2:]:
            if not isinstance(child, list) or not child:
                continue
            head_name = (
                str(child[0]) if isinstance(child[0], sexpdata.Symbol) else None
            )
            if head_name == "constraint" and len(child) >= 2:
                ctype = str(child[1])
                rule["constraint_type"] = ctype
                # Pull min/opt/max
                for clause in child[2:]:
                    if not (isinstance(clause, list) and len(clause) >= 2):
                        continue
                    chead = (
                        str(clause[0])
                        if isinstance(clause[0], sexpdata.Symbol)
                        else None
                    )
                    if chead in ("min", "opt", "max"):
                        rule[f"{chead}_value"] = _parse_dim(clause[1])
            elif head_name == "condition" and len(child) >= 2:
                rule["condition"] = (
                    child[1] if isinstance(child[1], str) else ""
                )
            elif head_name == "severity" and len(child) >= 2:
                sev = (
                    str(child[1])
                    if isinstance(child[1], sexpdata.Symbol)
                    else child[1]
                )
                rule["severity"] = str(sev)
            elif head_name == "layer" and len(child) >= 2:
                rule["layer"] = (
                    str(child[1])
                    if isinstance(child[1], sexpdata.Symbol)
                    else child[1]
                )
        rules.append(rule)
    return rules


def _parse_dim(value) -> str:
    """Coerce a parsed dimension token (e.g. '0.5mm') to a plain Python str.

    sexpdata reads `0.5mm` as a `Symbol` (subclass of str but with a custom
    `__eq__` that only compares Symbol→Symbol). We want plain `str` so
    callers can compare with `==` against literal strings.
    """
    if isinstance(value, sexpdata.Symbol):
        return str(value)
    if isinstance(value, str):
        return str(value)  # plain Python str, no Symbol leakage
    return str(value)


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


def _format_value_clauses(rule: dict) -> str:
    parts = []
    for key in ("min_value", "opt_value", "max_value"):
        if key in rule and rule[key] is not None:
            kw = key.split("_")[0]
            v = rule[key]
            if isinstance(v, (int, float)):
                v = f"{v}mm"
            parts.append(f"({kw} {v})")
    return " ".join(parts)


def _quote(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def render_rule(rule: dict) -> str:
    """Render one rule dict as the `(rule ...)` text block."""
    name = rule["name"]
    ctype = rule["constraint_type"]
    severity = rule.get("severity", "error")
    cond = rule.get("condition", "")
    layer = rule.get("layer", "")

    value_clauses = _format_value_clauses(rule)
    if value_clauses:
        constraint_line = f'    (constraint {ctype} {value_clauses})'
    else:
        constraint_line = f'    (constraint {ctype})'

    lines = [f'(rule "{_quote(name)}"', constraint_line]
    if cond:
        # Escape internal quotes minimally
        lines.append(f'    (condition "{_quote(cond)}")')
    if layer:
        lines.append(f'    (layer "{_quote(layer)}")')
    lines.append(f'    (severity {severity})')
    lines.append(")")
    return "\n".join(lines)


def _read_text(path: Path) -> str:
    return Path(path).read_bytes().decode("utf-8").replace("\r\n", "\n")


def _scan(text: str) -> tuple[list[tuple[int, int]], str]:
    """Spans of the top-level forms, and `text` with `#` comments blanked.

    KiCAD's rule files allow `#` comments, which sexpdata does not; they are
    blanked (same length) so the spans stay valid offsets into `text`.
    """
    spans: list[tuple[int, int]] = []
    out = list(text)
    depth, start, i, n, in_str = 0, 0, 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "#":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
            continue
        elif c == "(":
            if depth == 0:
                start = i
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                raise ValueError(f"unbalanced ')' at offset {i}")
            if depth == 0:
                spans.append((start, i + 1))
        i += 1
    if in_str or depth:
        raise ValueError("unterminated string or '(' at end of file")
    return spans, "".join(out)


def _rule_forms(text: str, path: Path | str = "") -> list[tuple[list, tuple[int, int]]]:
    """Every top-level `(rule ...)` form with its span in `text`."""
    try:
        spans, clean_text = _scan(text)
        out = []
        for s, e in spans:
            form = sexpdata.loads(clean_text[s:e])
            if is_call(form, "rule"):
                out.append((form, (s, e)))
        return out
    except Exception as exc:  # sexpdata raises several unrelated types
        raise ValueError(f"{path or 'rule file'} does not parse: {exc}") from exc


def _find_rule(text: str, name: str) -> tuple[int, int] | None:
    for form, span in _rule_forms(text):
        if len(form) >= 2 and isinstance(form[1], str) and form[1] == name:
            return span
    return None


def _save(path: Path, text: str) -> Path | None:
    from kicad_claude.adapters import safe_write

    path.parent.mkdir(parents=True, exist_ok=True)
    return safe_write.save_text(path, text, read_rules)


def set_rule(path: Path, rule: dict) -> dict:
    """Add `rule`, or replace the rule of the same name in place.

    Only that rule's text changes: comments, other rules and clauses this
    module does not model stay byte for byte.
    """
    path = Path(path)
    text = _read_text(path) if path.is_file() else "(version 1)\n"
    block = render_rule(rule)
    span = _find_rule(text, rule["name"])
    if span:
        text = text[:span[0]] + block + text[span[1]:]
    else:
        text = text.rstrip("\n") + "\n\n" + block + "\n"
    backup = _save(path, text)
    return {"replaced": span is not None, "total": len(_rule_forms(text)),
            "backup": str(backup) if backup else None}


def _cut(text: str, span: tuple[int, int]) -> str:
    """Remove a form, the rest of its line, and the blank line it leaves."""
    s, e = span
    rest = len(text[e:]) - len(text[e:].lstrip(" \t"))
    if text[e + rest:e + rest + 1] == "\n":
        e += rest + 1
    head, tail = text[:s], text[e:]
    if head.endswith("\n\n") and tail.startswith("\n"):
        tail = tail[1:]
    return head + tail


def remove_rule(path: Path, name: str) -> dict:
    """Delete the rule `name`. Raises `KeyError` when there is none."""
    path = Path(path)
    text = _read_text(path) if path.is_file() else ""
    span = _find_rule(text, name)
    if span is None:
        raise KeyError(f"no DRC rule named {name!r}")
    text = _cut(text, span)
    backup = _save(path, text)
    return {"remaining": len(_rule_forms(text)), "backup": str(backup) if backup else None}


def clear_rules(path: Path) -> dict:
    """Delete every rule; the version header and comments stay."""
    path = Path(path)
    if not path.is_file():
        return {"removed": 0, "backup": None}
    text = _read_text(path)
    forms = _rule_forms(text, path)
    for _form, span in reversed(forms):
        text = _cut(text, span)
    backup = _save(path, text) if forms else None
    return {"removed": len(forms), "backup": str(backup) if backup else None}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate_rule(rule: dict) -> None:
    """Raise ValueError on invalid rule dict."""
    if not rule.get("name"):
        raise ValueError("rule must have a non-empty name")
    ctype = rule.get("constraint_type", "")
    if not ctype:
        raise ValueError(f"rule {rule['name']!r} missing constraint_type")
    if ctype not in VALID_CONSTRAINTS:
        raise ValueError(
            f"unknown constraint_type {ctype!r}. Valid: {sorted(VALID_CONSTRAINTS)}"
        )
    sev = rule.get("severity", "error")
    if sev not in VALID_SEVERITIES:
        raise ValueError(
            f"severity must be one of {sorted(VALID_SEVERITIES)} (got {sev!r})"
        )
    # Some constraints don't need a min — `disallow` for instance has its own
    # syntax. We don't enforce min presence for that reason.
