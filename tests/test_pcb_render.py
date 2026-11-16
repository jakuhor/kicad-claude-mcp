"""2D board / sheet renders (OP-22)."""

from __future__ import annotations

import asyncio
import struct
import zlib
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import render2d, sch_io
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.utils.kicad_paths import find_kicad_cli


def _read_png(path: Path) -> tuple[int, int, bytes]:
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    idat, i = b"", 8
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        kind = data[i + 4:i + 8]
        if kind == b"IDAT":
            idat += data[i + 8:i + 8 + n]
        i += 12 + n
    return w, h, zlib.decompress(idat)


def test_png_writer_round_trips(tmp_path):
    w, h, stride = 3, 2, 12                       # stride wider than a row
    buf = bytearray(stride * h)
    buf[0:3] = b"\xff\x00\x00"
    buf[stride + 6:stride + 9] = b"\x00\x00\xff"
    render2d.write_png(tmp_path / "t.png", w, h, stride, buf, 3)
    rw, rh, raw = _read_png(tmp_path / "t.png")
    assert (rw, rh) == (3, 2)
    assert raw == b"\x00" + bytes(buf[0:9]) + b"\x00" + bytes(buf[stride:stride + 9])


def test_draw_order_puts_the_viewed_side_last():
    layers = ["F.Cu", "B.Cu", "F.SilkS", "Edge.Cuts", "B.SilkS"]
    assert render2d.draw_order(layers, False) == ["B.Cu", "B.SilkS", "F.Cu", "F.SilkS", "Edge.Cuts"]
    assert render2d.draw_order(layers, True) == ["F.Cu", "F.SilkS", "B.Cu", "B.SilkS", "Edge.Cuts"]


@pytest.fixture
def project(tmp_path):
    if not find_kicad_cli():
        pytest.skip("no kicad-cli")
    state.clear_active()
    files = write_blank_project(tmp_path / "rd", "rd")
    tree = sch_io.parse_file(files["pcb"])
    ed.set_board_outline(tree, 40, 30)
    sch_io.write_file(files["pcb"], tree)
    state.set_active(tmp_path / "rd", "rd")
    yield files
    state.clear_active()


def _mcp():
    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools.pcb import layout as pcb_layout
    from kicad_claude.tools.sch import layout as sch_layout
    mcp = FastMCP("t")
    pcb_layout.register(mcp)
    sch_layout.register(mcp)
    return mcp


@pytest.mark.slow
def test_render_board_is_cut_to_the_outline(project, tmp_path):
    out = asyncio.run(_mcp().call_tool("render_board", {"out_path": str(tmp_path / "b.png")}))
    content = out[0] if isinstance(out, tuple) else out
    assert content[0].type == "image" and content[0].mimeType == "image/png"
    w, h, _ = _read_png(tmp_path / "b.png")
    # 42 x 32 mm (outline + 1 mm each side) at 400 dpi
    assert abs(w - 42 / 25.4 * 400) <= 2 and abs(h - 32 / 25.4 * 400) <= 2


@pytest.mark.slow
def test_render_sheet_and_page_range(project, tmp_path):
    mcp = _mcp()
    fn = mcp._tool_manager.get_tool("render_sheet").fn
    img, info = fn(out_path=str(tmp_path / "s.png"), dpi=50)
    assert info["page"] == 1 and info["width_px"] > info["height_px"]   # A4 landscape
    with pytest.raises(ValueError, match="out of range"):
        fn(page=2)
