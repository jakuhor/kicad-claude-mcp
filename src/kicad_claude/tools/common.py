"""Active-project helpers shared by several tool sets (not a tool set itself).

    project_netlist  — KiCAD's kicadsexpr netlist of the active project
    datasheet_values — regulator Vref / limits from the extracted spec cache
    specs_by_ref     — fresh spec records per reference
    snapshot         — a revision (`git:<ref>`, folder, or working tree) on disk
    project_files    — the .kicad_sch / .kicad_pcb pair inside a folder
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import re
import typing
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import design_diff, kicad_cli, power_review


def project_netlist(netlist_path: str = "") -> Path:
    """`netlist_path` if given, else a fresh kicadsexpr export of the active
    project to `fab/<name>-netlist.net`."""
    if netlist_path:
        p = Path(netlist_path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(p)
        return p
    proj = state.get_active()
    out = proj.path / "fab" / f"{proj.name}-netlist.net"
    out.parent.mkdir(parents=True, exist_ok=True)
    kicad_cli.export_netlist(proj.sch_path, out, fmt="kicadsexpr")
    return out


def datasheet_values(nl) -> dict:
    """Per regulator name: Vref (+ negative) and ANY-OUT weights from
    `datasheets/specs/<MPN>.json`, each as (value, "datasheet MPN p.N (file)").
    Channel values (regulator.channels.<ch>) win over part-wide ones; stale
    records are ignored."""
    from kicad_claude.adapters import datasheet_index as dsi
    from kicad_claude.adapters import datasheet_specs as dss

    proj = state.get_active_or_none()
    if proj is None:
        return {}
    ds_dir = dsi.datasheets_dir(proj.path)
    out: dict = {}
    for reg in power_review.detect_regulators(nl):
        ref, ch = reg["ref"], reg["channel"]
        mpn = reg["comp"]["fields"].get("MPN", "")
        rec = dss.load(ds_dir, mpn) if mpn else None
        if not rec or rec.get("stale"):
            continue
        r = rec.get("regulator") or {}
        chan = (r.get("channels") or {}).get(ch) or {}

        def src(sv, mpn=mpn, rec=rec):
            return f"datasheet {mpn} p.{sv['page']} ({rec['pdf']['file']})"

        vals: dict = {}
        for key in ("vref", "vref_negative", "set_current"):
            sv = chan.get(key) or r.get(key)
            v = dss.value(sv)
            if v is not None:
                vals[key] = (v, src(sv))
        ip = chan.get("input_pin") or r.get("input_pin")
        if ip:
            vals["input_pin"] = (ip, f"datasheet {mpn} ({rec['pdf']['file']})")
        io = {**(r.get("internal_outputs") or {}), **(chan.get("internal_outputs") or {})}
        if io:
            vals["internal_outputs"] = {
                pin: (x["above_pin"], dss.value(x["offset"]), src(x["offset"]))
                for pin, x in io.items() if dss.value(x.get("offset")) is not None}
        ao = chan.get("anyout") or r.get("anyout")
        if ao and dss.value(ao.get("base")) is not None:
            vals["anyout"] = {"base": (dss.value(ao["base"]), src(ao["base"])),
                              "pins": {n: (dss.value(sv), src(sv))
                                       for n, sv in ao["pins"].items()
                                       if dss.value(sv) is not None}}
        lim = {}
        absmax, rec_ = rec.get("absolute_max") or {}, rec.get("recommended") or {}
        for key, sv in (("abs_vin", absmax.get("VIN")), ("rec_vin", rec_.get("VIN")),
                        ("abs_vout", absmax.get("VOUT")), ("rec_vout", rec_.get("VOUT")),
                        ("abs_en", absmax.get("EN")),
                        ("en_on", chan.get("en_threshold_rising") or r.get("en_threshold_rising")),
                        ("dropout", chan.get("dropout") or r.get("dropout"))):
            if sv:
                lim[key] = (sv.get("min"), sv.get("typ"), sv.get("max"), src(sv))
        if lim:
            vals["limits"] = lim
        if vals:
            out[f"{ref}{'/ch' + ch if ch else ''}"] = vals
    return out


def specs_by_ref(nl) -> dict:
    """{ref: spec record} for every part whose MPN has a fresh extraction."""
    from kicad_claude.adapters import datasheet_index as dsi
    from kicad_claude.adapters import datasheet_specs as dss

    proj = state.get_active_or_none()
    if proj is None:
        return {}
    ds_dir = dsi.datasheets_dir(proj.path)
    cache: dict = {}
    out = {}
    for ref, comp in nl.components.items():
        mpn = comp["fields"].get("MPN", "")
        if not mpn:
            continue
        if mpn not in cache:
            cache[mpn] = dss.load(ds_dir, mpn)
        rec = cache[mpn]
        if rec and not rec.get("stale"):
            out[ref] = rec
    return out


def snapshot(spec: str, tmp: Path) -> Path:
    """`spec` is a folder, `git:<ref>` of the active project, or empty (= the
    active project's working tree)."""
    proj = state.get_active()
    if not spec:
        return proj.path
    if spec.startswith("git:"):
        d = tmp / re.sub(r"[^\w.-]", "_", spec)
        d.mkdir()
        return design_diff.snapshot_git(proj.path, spec[4:], d)
    p = Path(spec).expanduser().resolve()
    if not p.is_dir():
        raise FileNotFoundError(p)
    return p


def project_files(folder: Path, name: str) -> tuple[Path, Path]:
    sch = folder / f"{name}.kicad_sch"
    pcb = folder / f"{name}.kicad_pcb"
    if not sch.is_file():
        cands = [p for p in folder.glob("*.kicad_pro")]
        if cands:
            sch = cands[0].with_suffix(".kicad_sch")
            pcb = cands[0].with_suffix(".kicad_pcb")
    return sch, pcb


# --------------------------------------------------------------------------- #
# Optional `sheet` / `board` argument
# --------------------------------------------------------------------------- #


def resolve_sheet_path(proj, sheet: str) -> Path:
    """A sheet given as "root", a .kicad_sch filename, or a root sheet name."""
    from kicad_claude.adapters import sch_editor as ed
    from kicad_claude.adapters import sch_io

    if sheet.lower() == "root":
        return Path(proj.sch_path)
    if sheet.endswith(".kicad_sch"):
        p = proj.path / sheet
        if not p.is_file():
            raise FileNotFoundError(f"{p} does not exist")
        return p
    node = ed.find_sheet_by_name(sch_io.parse_file(proj.sch_path), sheet)
    if node is None:
        raise KeyError(f"no sheet named {sheet!r} in root")
    filename = ed.get_sheet_filename(node)
    if not filename:
        raise RuntimeError(f"sheet {sheet!r} has no Sheetfile")
    return proj.path / filename


def _with_extra_arg(fn, name: str, enter):
    """`fn` with an extra optional `name: str = ""` argument; a non-empty value
    runs the call inside `enter(value)`. The signature is rebuilt with
    evaluated annotations so FastMCP sees every argument's real type."""
    hints = typing.get_type_hints(fn)
    sig = inspect.signature(fn)
    if name in sig.parameters:
        raise TypeError(f"{fn.__name__} already has a {name!r} argument")
    params = [p.replace(annotation=hints.get(p.name, p.annotation))
              for p in sig.parameters.values()]
    params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY,
                                    default="", annotation=str))

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        value = kwargs.pop(name, "")
        if not value:
            return fn(*args, **kwargs)
        with enter(value):
            return fn(*args, **kwargs)

    wrapper.__signature__ = sig.replace(
        parameters=params, return_annotation=hints.get("return", sig.return_annotation))
    wrapper.__annotations__ = {**hints, name: str}
    return wrapper


@contextlib.contextmanager
def _sheet(value: str):
    proj = state.get_active()
    path = resolve_sheet_path(proj, value)
    rel = path.resolve().relative_to(proj.path.resolve()).as_posix()
    with state.using_sheet(None if path.resolve() == proj.sch_path.resolve() else rel):
        yield


@contextlib.contextmanager
def _board(value: str):
    with state.using_board(None if value.lower() == "main" else value):
        yield


def on_sheet(fn):
    """Give a sheet-level tool an optional `sheet` ("root", file or sheet name)."""
    return _with_extra_arg(fn, "sheet", _sheet)


def on_board(fn):
    """Give a board-level tool an optional `board` ("main" or a .kicad_pcb file)."""
    return _with_extra_arg(fn, "board", _board)
