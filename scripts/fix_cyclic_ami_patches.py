#!/usr/bin/env python3
"""Upgrade regional AMI wall patches to cyclicAMI with coupling parameters."""

from __future__ import annotations

import json
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

from foam2thermal.interfaces import is_ami_patch  # noqa: E402
from foam2thermal.mesh import (  # noqa: E402
    PatchInfo,
    boundary_header_text,
    cyclic_ami_patch,
    parse_boundary,
    write_boundary,
)


def _ami_pairs(case: Path) -> list[tuple[str, str]]:
    cfg_path = case / "config.json"
    if cfg_path.is_file():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        explicit = raw.get("interfaces", {}).get("explicit", [])
        pairs = [(e["master"], e["slave"]) for e in explicit if e.get("method") == "cyclicAMI"]
        if pairs:
            return pairs
    report = case / "setup_report.json"
    if report.is_file():
        data = json.loads(report.read_text(encoding="utf-8"))
        return [
            (i["master"], i["slave"])
            for i in data.get("interfaces", [])
            if i.get("method") == "cyclicAMI"
        ]
    return []


def _rotation_axis(case: Path) -> tuple[float, float, float]:
    cfg_path = case / "config.json"
    if cfg_path.is_file():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        axis = raw.get("interfaces", {}).get("ami_rotation_axis", [0, 0, 1])
        return (float(axis[0]), float(axis[1]), float(axis[2]))
    return (0.0, 0.0, 1.0)


def _partner(name: str, pairs: list[tuple[str, str]]) -> str | None:
    for m, s in pairs:
        if name == m:
            return s
        if name == s:
            return m
    return None


def fix_case(case: Path) -> int:
    pairs = _ami_pairs(case)
    if not pairs:
        print(f"No cyclicAMI pairs configured in {case}")
        return 0

    axis = _rotation_axis(case)
    tol = 0.001
    n_fixed = 0

    for bnd in sorted(case.glob("constant/*/polyMesh/boundary")):
        patches = parse_boundary(bnd)
        names = {p.name for p in patches}
        changed = False
        new_patches: list[PatchInfo] = []

        for p in patches:
            neighbour = _partner(p.name, pairs)
            needs_fix = False
            if (
                p.patch_type == "wall"
                and neighbour
                and neighbour in names
                and is_ami_patch(p.name, [r"ami_rot\d+"])
            ):
                # Upgrade wall -> cyclicAMI
                needs_fix = True
            elif (
                p.patch_type == "cyclicAMI"
                and neighbour
                and neighbour in names
                and (
                    not p.neighbour_patch
                    or p.neighbour_patch in ("None", "none", "")
                    or p.neighbour_patch != neighbour
                )
            ):
                # Fix cyclicAMI with missing/wrong neighbourPatch
                needs_fix = True

            if needs_fix:
                new_patches.append(
                    cyclic_ami_patch(
                        p.name,
                        neighbour,
                        n_faces=p.n_faces,
                        start_face=p.start_face,
                        rotation_axis=axis,
                        match_tolerance=tol,
                    )
                )
                changed = True
                n_fixed += 1
            else:
                new_patches.append(p)

        if changed:
            write_boundary(bnd, new_patches, boundary_header_text(bnd))
            print(f"updated {bnd}")

    print(f"fixed {n_fixed} AMI patch(es) in {case}")
    return 0


def main() -> int:
    case = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    return fix_case(case)


if __name__ == "__main__":
    raise SystemExit(main())
