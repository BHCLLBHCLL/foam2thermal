#!/usr/bin/env python3
"""Upgrade regional *_to_* polyMesh patches from wall to mappedWall."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _find_src() -> Path:
    here = Path(__file__).resolve().parent
    for base in (here, *here.parents):
        src = base / "src"
        if (src / "foam2thermal" / "mesh.py").is_file():
            return src
    raise RuntimeError("foam2thermal package not found")


sys.path.insert(0, str(_find_src()))

from foam2thermal.mesh import (  # noqa: E402
    PatchInfo,
    boundary_header_text,
    parse_boundary,
    parse_coupling_patch,
    write_boundary,
)


def _region_names(case: Path) -> list[str]:
    for rel in ("constant/regionProperties", "system/regionProperties"):
        rp = case / rel
        if not rp.is_file():
            continue
        text = rp.read_text(encoding="utf-8", errors="replace")
        names: list[str] = []
        for block in ("fluid", "solid"):
            m = re.search(rf"{block}\s*\(\s*([^)]+)\)", text, flags=re.DOTALL)
            if not m:
                continue
            chunk = m.group(1).strip()
            quoted = re.findall(r'"([^"]+)"', chunk)
            names.extend(quoted if quoted else chunk.split())
        if names:
            return names
    return sorted(p.parent.parent.name for p in case.glob("constant/*/polyMesh/boundary"))


def fix_case(case: Path) -> int:
    regions = _region_names(case)
    n_fixed = 0
    for bnd in case.glob("constant/*/polyMesh/boundary"):
        patches = parse_boundary(bnd)
        changed = False
        new_patches: list[PatchInfo] = []
        for p in patches:
            if "_to_" not in p.name or (
                p.patch_type == "mappedWall"
                and p.sample_region
                and p.sample_region not in ("None", "none")
            ):
                new_patches.append(p)
                continue
            parsed = parse_coupling_patch(p.name, regions)
            if not parsed:
                new_patches.append(p)
                continue
            local, remote = parsed
            new_patches.append(
                PatchInfo(
                    name=p.name,
                    patch_type="mappedWall",
                    n_faces=p.n_faces,
                    start_face=p.start_face,
                    sample_mode="nearestPatchFace",
                    sample_region=remote,
                    sample_patch=f"{remote}_to_{local}",
                )
            )
            changed = True
            n_fixed += 1
        if changed:
            write_boundary(bnd, new_patches, boundary_header_text(bnd))
            print(f"updated {bnd}")
    print(f"fixed {n_fixed} coupling patch(es) in {case}")
    return 0


def main() -> int:
    case = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    return fix_case(case)


if __name__ == "__main__":
    raise SystemExit(main())
