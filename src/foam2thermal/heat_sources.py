"""Map post-split region heat loads from JSON boundary_conditions."""

from __future__ import annotations

import re
from typing import Any


def parse_uniform_scalar(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = re.search(r"uniform\s+([\d.eE+-]+)", text)
    if match:
        return float(match.group(1))
    return float(text)


def region_power_watts(boundary_conditions: dict[str, Any], region_name: str) -> float | None:
    """Return total power (W) from externalWallHeatFluxTemperature mode=power entries."""
    rbc = boundary_conditions.get(region_name, {})
    t_bc = rbc.get("T", {})
    total = 0.0
    found = False
    for spec in t_bc.values():
        if not isinstance(spec, dict):
            continue
        if (
            spec.get("type") == "externalWallHeatFluxTemperature"
            and spec.get("mode") == "power"
        ):
            total += parse_uniform_scalar(spec.get("Q", 0))
            found = True
    return total if found else None
