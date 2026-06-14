#!/usr/bin/env python3
"""Split monolithic polyMesh into regional meshes (Windows-safe)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _find_src() -> Path:
    here = Path(__file__).resolve().parent
    for base in (here, *here.parents):
        src = base / "src"
        if (src / "foam2thermal" / "mesh_split.py").is_file():
            return src
    raise RuntimeError("foam2thermal package not found (set PYTHONPATH to project src/)")


sys.path.insert(0, str(_find_src()))

from foam2thermal.mesh_split import split_mesh_regions  # noqa: E402


def main() -> int:
    case = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    report = split_mesh_regions(case)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
