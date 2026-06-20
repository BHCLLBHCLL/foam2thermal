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


def _update_mrf_non_rotating(case: Path, regions: list[str]) -> int:
    """Add post-split coupling patches to MRFProperties.nonRotatingPatches.

    The MRFProperties template is generated from the monolithic mesh (before
    splitMeshRegions), so it only knows about the original patches (open, AMI,
    impeller).  After split, new ``*_to_*`` coupling patches appear and must be
    listed as nonRotating so the MRF does not try to rotate the mappedWall
    interfaces to stationary solid regions.
    """
    mrf = case / "constant" / "air" / "MRFProperties"
    if not mrf.is_file():
        return 0
    text = mrf.read_text(encoding="utf-8", errors="replace")

    # Gather coupling patches from the air region boundary
    bnd = case / "constant" / "air" / "polyMesh" / "boundary"
    if not bnd.is_file():
        return 0
    coupling = [p.name for p in parse_boundary(bnd) if "_to_" in p.name]
    if not coupling:
        return 0

    changed = False
    out_lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^(\s*)nonRotatingPatches\s*\(\s*([^)]*)\s*\)\s*;?\s*$", line)
        if not m:
            out_lines.append(line)
            continue
        indent = m.group(1)
        existing = m.group(2).split()
        merged = sorted(set(existing) | set(coupling))
        out_lines.append(f"{indent}nonRotatingPatches ( {' '.join(merged)} );")
        changed = True
    if changed:
        mrf.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        print(f"updated {mrf} (added {len(coupling)} coupling patch(es))")
    return 0


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
    _update_mrf_non_rotating(case, regions)
    return 0


def main() -> int:
    case = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    return fix_case(case)


if __name__ == "__main__":
    raise SystemExit(main())
