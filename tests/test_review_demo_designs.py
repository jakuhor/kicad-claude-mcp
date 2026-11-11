"""Circuit detectors on KiCAD's bundled demo designs (OP-12).

Real schematics from other designers: the detectors must not raise errors
on them (every error found while building this was a false positive), and
the warnings stay few. Slow: needs kicad-cli and the KiCAD demos folder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_claude.adapters import circuit_review, kicad_cli
from kicad_claude.adapters import power_review as pr
from kicad_claude.utils.kicad_paths import find_kicad_cli

DEMOS = ["cm5_minima/CM5_MINIMA_3", "jetson-agx-thor-baseboard/jetson-agx-thor-baseboard",
         "openair-max/One-Air-Max", "royalblue54L_feather/RoyalBlue54L-Feather",
         "stickhub/StickHub", "video/video", "kit-dev-coldfire-xilinx_5213/kit-dev-coldfire-xilinx_5213",
         "interf_u/interf_u", "pic_programmer/pic_programmer", "tiny_tapeout/tinytapeout-demo",
         "multichannel/multichannel_mixer"]


def _demos_dir() -> Path | None:
    cli = find_kicad_cli()
    if cli is None:
        return None
    d = Path(cli).parent.parent / "share" / "kicad" / "demos"
    return d if d.is_dir() else None


@pytest.mark.slow
@pytest.mark.parametrize("demo", DEMOS)
def test_no_errors_on_demo(demo, tmp_path):
    d = _demos_dir()
    if d is None or not (d / f"{demo}.kicad_sch").is_file():
        pytest.skip("KiCAD demos not installed")
    net = tmp_path / "d.net"
    kicad_cli.export_netlist(d / f"{demo}.kicad_sch", net, fmt="kicadsexpr")
    nl = pr.parse_netlist(net)
    r = circuit_review.analyze(nl, pr.analyze(nl), {})
    errors = [f"{f['rule']} {f['ref']}: {f['message']}" for f in r["findings"]
              if f["severity"] == "error"]
    assert errors == []
    assert r["counts"]["warning"] <= 10
