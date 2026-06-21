#!/usr/bin/env python3
"""Fix cgns2foam cellZones bug: multiple zones pointing to the same cells.

The cgns2foam tool has a bug where it writes the same cell_labels (1804 cells
of the CPU zone) for multiple zones (CPU, Cu_block, Cover, fin_1, fin_2,
rotation1). This script reads the correct zone cell counts from the CGNS
file and rewrites the cellZones file with contiguous cell ranges.

Usage:
    python scripts/fix_cell_zones_from_cgns.py <cgns_file> <polyMesh_dir>

Example:
    python scripts/fix_cell_zones_from_cgns.py \
        D:/training/cgns/cgns2foam/tests/laptop_thermal_steady_scaled_v3_orig_fix.cgns \
        tests/laptop_thermal_steady_scaled_v3_orig/constant/polyMesh
"""

from __future__ import annotations

import sys
from pathlib import Path

import h5py
import numpy as np


def _read_cgns_zone_ranges(cgns_path: Path) -> list[tuple[str, int, int]]:
    """Return list of (zone_name, start_cell, end_cell) from CGNS file.

    Each CGNS Zone_t corresponds to a contiguous cell range in the
    cgns2foam-generated OpenFOAM mesh, in the order zones appear in the
    CGNS Base.
    """
    zones: list[tuple[str, int, int]] = []
    with h5py.File(cgns_path, "r") as f:
        base = f["Base"]
        cursor = 0
        for key in base.keys():
            item = base[key]
            if not isinstance(item, h5py.Group):
                continue
            data = item.get(" data")
            if data is None:
                continue
            size = data[()]
            # size = [[n_vertices], [n_cells], [0]]
            n_cells = int(size[1][0])
            zones.append((key, cursor, cursor + n_cells - 1))
            cursor += n_cells
    return zones


def _build_cell_labels(start: int, end: int) -> np.ndarray:
    """Return contiguous label array [start, start+1, ..., end]."""
    return np.arange(start, end + 1, dtype=np.int32)


def fix_cell_zones(cgns_path: Path, poly_dir: Path) -> dict:
    """Rewrite cellZones in *poly_dir* using zone ranges from *cgns_path*."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from foam2thermal.mesh import CellZoneInfo, write_cell_zones_v2412

    zones = _read_cgns_zone_ranges(cgns_path)
    cell_zones: list[CellZoneInfo] = []
    summary: list[dict] = []
    for name, start, end in zones:
        labels = _build_cell_labels(start, end)
        cell_zones.append(CellZoneInfo(name=name, cell_labels=labels.tolist()))
        summary.append({"name": name, "start": start, "end": end, "n_cells": len(labels)})

    cz_path = poly_dir / "cellZones"
    if not cz_path.is_file():
        raise FileNotFoundError(f"cellZones not found: {cz_path}")

    backup = cz_path.with_suffix(".bak")
    if not backup.exists():
        backup.write_bytes(cz_path.read_bytes())

    write_cell_zones_v2412(cz_path, cell_zones)
    return {
        "cgns_file": str(cgns_path),
        "cellZones_file": str(cz_path),
        "backup_file": str(backup),
        "zones": summary,
        "total_cells": sum(z["n_cells"] for z in summary),
    }


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cgns_path = Path(sys.argv[1]).resolve()
    poly_dir = Path(sys.argv[2]).resolve()
    if not cgns_path.is_file():
        print(f"CGNS file not found: {cgns_path}", file=sys.stderr)
        return 1
    if not poly_dir.is_dir():
        print(f"polyMesh directory not found: {poly_dir}", file=sys.stderr)
        return 1
    import json
    report = fix_cell_zones(cgns_path, poly_dir)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
