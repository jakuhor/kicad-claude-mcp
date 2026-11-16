"""2D PNG renders of a board or a schematic sheet.

kicad-cli plots a PDF (`pcb export pdf --mode-single`, `sch export pdf
--pages N`); `pypdfium2` rasterizes it and a small writer below stores the
PNG, so no imaging library is needed.

A board plotted at scale 1 lands on the page at its own coordinates: board
millimetre (x, y) is page millimetre (x, y) from the top-left corner, and
with `--mirror` x becomes `page width - x`. The render is cut to the board
outline (or a caller's region) by moving the PDF media box there, which
also reaches a board that sits partly off the drawing sheet.
"""

from __future__ import annotations

import struct
import tempfile
import zlib
from pathlib import Path

from kicad_claude.adapters import kicad_cli

PT_PER_MM = 72 / 25.4
MAX_PIXELS = 4000          # longest side; the dpi is lowered to fit
DEFAULT_LAYERS = ("F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "F.Fab", "Edge.Cuts")


def draw_order(layers: list[str], bottom: bool) -> list[str]:
    """kicad-cli paints the layers in list order: the far side first, then
    the side looked at, then side-less layers (Edge.Cuts, User.*)."""
    near, far = ("B.", "F.") if bottom else ("F.", "B.")

    def key(name: str) -> int:
        return 0 if name.startswith(far) else 1 if name.startswith(near) else 2
    return sorted(layers, key=key)


def write_png(path: Path, width: int, height: int, stride: int, buf, channels: int) -> None:
    """RGB / RGBA 8-bit PNG from a row-strided buffer."""
    mv = memoryview(buf).cast("B")
    row = width * channels
    raw = b"".join(b"\x00" + bytes(mv[y * stride:y * stride + row]) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    color = 2 if channels == 3 else 6
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n"
                           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def rasterize(pdf: Path, out_png: Path, *, page: int = 0, dpi: float = 200,
              region_mm: tuple[float, float, float, float] | None = None,
              mirror: bool = False) -> dict:
    """Render one PDF page (or `region_mm` = x1, y1, x2, y2 in page mm from
    the top-left) to `out_png`. Returns size and the dpi actually used."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    try:
        pg = doc[page]
        w_pt, h_pt = pg.get_size()
        if region_mm:
            x1, y1, x2, y2 = region_mm
            if mirror:
                pw = w_pt / PT_PER_MM
                x1, x2 = pw - x2, pw - x1
            box = (x1 * PT_PER_MM, h_pt - y2 * PT_PER_MM, x2 * PT_PER_MM, h_pt - y1 * PT_PER_MM)
            pg.set_mediabox(*box)
            pg.set_cropbox(*box)
            w_pt, h_pt = box[2] - box[0], box[3] - box[1]
        scale = dpi / 72
        longest = max(w_pt, h_pt) * scale
        if longest > MAX_PIXELS:
            scale *= MAX_PIXELS / longest
        bm = pg.render(scale=scale, rev_byteorder=True, fill_color=(255, 255, 255, 255))
        write_png(out_png, bm.width, bm.height, bm.stride, bm.buffer, bm.n_channels)
        return {"png": str(out_png), "width_px": bm.width, "height_px": bm.height,
                "dpi": round(scale * 72, 1), "pages": len(doc)}
    finally:
        doc.close()


def render_board(pcb: Path, out_png: Path, *, layers: list[str] | None = None,
                 region_mm: tuple[float, float, float, float] | None = None,
                 board_box: tuple[float, float, float, float] | None = None,
                 mirror: bool = False, dpi: float = 200, margin_mm: float = 1.0) -> dict:
    """Board layers to PNG, cut to `region_mm`, else `board_box` + margin."""
    box = region_mm or (board_box and (board_box[0] - margin_mm, board_box[1] - margin_mm,
                                       board_box[2] + margin_mm, board_box[3] + margin_mm))
    with tempfile.TemporaryDirectory(prefix="kc-render-") as tmp:
        pdf = Path(tmp) / "board.pdf"
        layers = draw_order(list(layers or DEFAULT_LAYERS), mirror)
        args = ["pcb", "export", "pdf", "--mode-single",
                "--layers", ",".join(layers), "-o", str(pdf)]
        if mirror:
            args.append("--mirror")
        kicad_cli._run(args + [str(pcb)], timeout=120)
        result = rasterize(pdf, out_png, dpi=dpi, region_mm=box, mirror=mirror)
    result.pop("pages", None)
    result.update(layers=layers, region_mm=list(box) if box else None,
                  mirror=mirror)
    return result


def render_sheet(sch: Path, out_png: Path, *, page: int = 1,
                 region_mm: tuple[float, float, float, float] | None = None,
                 dpi: float = 150) -> dict:
    """Schematic page `page` (1-based, the PDF's page order) to PNG."""
    with tempfile.TemporaryDirectory(prefix="kc-render-") as tmp:
        pdf = Path(tmp) / "sheet.pdf"
        kicad_cli._run(["sch", "export", "pdf", "--exclude-pdf-property-popups",
                        "-o", str(pdf), str(sch)], timeout=120)
        import pypdfium2 as pdfium
        n = len(pdfium.PdfDocument(str(pdf)))
        if not 1 <= page <= n:
            raise ValueError(f"page {page} out of range: the schematic has {n} page(s)")
        result = rasterize(pdf, out_png, page=page - 1, dpi=dpi, region_mm=region_mm)
    result.update(page=page, region_mm=list(region_mm) if region_mm else None)
    return result
