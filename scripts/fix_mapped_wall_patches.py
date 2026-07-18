#!/usr/bin/env python3
"""Post-split mesh/BC hygiene for multi-region CHT cases.

- Upgrade ``*_to_*`` polyMesh patches from ``wall`` to ``mappedWall``
- Force ``open*`` patch type from ``wall`` → ``patch`` (free openings)
- Merge AMI + open + coupling names into ``MRFProperties.nonRotatingPatches``
  (also updates ``constant.orig/air`` so Allrun.pre ``cp`` cannot wipe it)
"""

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
    resolve_open_patch_type,
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
    """Merge AMI + open + coupling patches into MRFProperties.nonRotatingPatches.

    The MRFProperties template is generated from the monolithic mesh (before
    split), so post-split ``*_to_*`` patches must be added.  Both AMI sides and
    all ``open*`` patches are re-asserted here in case the build-time list was
    incomplete (e.g. ``_PartSurface_air_domain_7`` not matching ami patterns).

    Updates both ``constant/air`` and ``constant.orig/air`` so a later
    ``cp constant.orig → constant`` in AllrunPrep cannot wipe the merge.
    """
    _ = regions
    bnd = case / "constant" / "air" / "polyMesh" / "boundary"
    if not bnd.is_file():
        return 0
    extras: list[str] = []
    for p in parse_boundary(bnd):
        if "_to_" in p.name:
            extras.append(p.name)
        elif p.patch_type == "cyclicAMI":
            extras.append(p.name)
        elif p.name == "open" or p.name.startswith("open"):
            extras.append(p.name)
    if not extras:
        return 0

    for rel in (
        Path("constant") / "air" / "MRFProperties",
        Path("constant.orig") / "air" / "MRFProperties",
    ):
        mrf = case / rel
        if not mrf.is_file():
            continue
        text = mrf.read_text(encoding="utf-8", errors="replace")
        changed = False
        out_lines: list[str] = []
        for line in text.splitlines():
            m = re.match(r"^(\s*)nonRotatingPatches\s*\(\s*([^)]*)\s*\)\s*;?\s*$", line)
            if not m:
                out_lines.append(line)
                continue
            indent = m.group(1)
            existing = m.group(2).split()
            merged = sorted(set(existing) | set(extras))
            out_lines.append(f"{indent}nonRotatingPatches ( {' '.join(merged)} );")
            if merged != sorted(set(existing)):
                changed = True
        if changed:
            mrf.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
            print(f"updated {mrf} (nonRotating += {len(extras)} patch(es))")
    return 0


def fix_case(case: Path) -> int:
    regions = _region_names(case)
    n_fixed = 0
    n_open = 0
    for bnd in case.glob("constant/*/polyMesh/boundary"):
        patches = parse_boundary(bnd)
        changed = False
        new_patches: list[PatchInfo] = []
        for p in patches:
            # cgns2foam marks open* as wall; force type=patch (free opening).
            resolved = resolve_open_patch_type(p.name, p.patch_type)
            if resolved != p.patch_type:
                p = PatchInfo(
                    name=p.name,
                    patch_type=resolved,
                    n_faces=p.n_faces,
                    start_face=p.start_face,
                    sample_mode=p.sample_mode,
                    sample_region=p.sample_region,
                    sample_patch=p.sample_patch,
                    neighbour_patch=p.neighbour_patch,
                    rotation_axis=p.rotation_axis,
                    match_tolerance=p.match_tolerance,
                    transform=p.transform,
                )
                changed = True
                n_open += 1

            # Never rewrite non-coupling patches (preserves cyclicAMI metadata).
            if "_to_" not in p.name:
                new_patches.append(p)
                continue
            if (
                p.patch_type == "mappedWall"
                and p.sample_region
                and p.sample_region not in ("None", "none")
                and p.sample_patch
                and p.sample_patch not in ("None", "none")
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
                    neighbour_patch=p.neighbour_patch,
                    rotation_axis=p.rotation_axis,
                    match_tolerance=p.match_tolerance,
                    transform=p.transform,
                )
            )
            changed = True
            n_fixed += 1
        if changed:
            write_boundary(bnd, new_patches, boundary_header_text(bnd))
            print(f"updated {bnd}")
    print(f"fixed {n_fixed} coupling patch(es), {n_open} open->patch in {case}")
    _update_mrf_non_rotating(case, regions)
    return 0


def main() -> int:
    case = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    return fix_case(case)


if __name__ == "__main__":
    raise SystemExit(main())
