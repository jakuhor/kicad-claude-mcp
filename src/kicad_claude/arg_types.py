"""Argument types shared by tool signatures.

A `Literal` lists its choices in the tool's JSON schema, so the model sees
them without reading the description. Each one mirrors a tuple in an adapter;
`tests/test_core_toolsets.py` keeps the two in step.
"""

from __future__ import annotations

from typing import Iterable, Literal

Source = Literal["digikey", "mouser", "tme", "farnell", "jlcpcb"]
EmcGroup = Literal["ground", "switching", "decoupling", "io", "thermal"]
CircuitGroup = Literal["power", "analog", "digital", "misc", "interfaces",
                       "power_conversion", "drives", "rf"]
ConnectivityCheck = Literal["power_pins", "decoupling", "open_drain", "i2c",
                            "datasheet_pins", "pin_abs_max", "cap_voltage"]
SubcircuitType = Literal["divider", "rc", "lc", "opamp", "crystal"]
DeratingProfile = Literal["commercial", "industrial", "automotive", "military"]


def clean(items: Iterable[str] | None, default: Iterable[str] = ()) -> list[str]:
    """Stripped, non-empty entries of a list argument; `default` when none."""
    out = [str(i).strip() for i in (items or ()) if str(i).strip()]
    return out or list(default)
