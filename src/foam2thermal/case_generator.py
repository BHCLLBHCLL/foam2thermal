"""Generate a complete chtMultiRegionSimpleFoam case from JSON config."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .config import CaseConfig
from .interfaces import InterfaceMethod, build_interface_list
from .mesh import load_mesh, validate_mesh_complete
from .templates import (
    control_dict,
    create_baffles_ami,
    field_p,
    field_p_rgh,
    field_T,
    field_U,
    fv_schemes_fluid,
    fv_schemes_solid,
    fv_solution_fluid,
    fv_solution_solid,
    gravity_vector,
    region_properties,
    thermophysical_fluid,
    thermophysical_solid,
    turbulence_properties,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _copy_mesh(source: Path, dest: Path) -> None:
    src_poly = source / "constant" / "polyMesh"
    dst_poly = dest / "constant" / "polyMesh"
    if dst_poly.exists():
        shutil.rmtree(dst_poly)
    shutil.copytree(src_poly, dst_poly)


def _infer_patch_region(patch: str, cfg: CaseConfig) -> str | None:
    if patch in cfg.patch_regions:
        return cfg.patch_regions[patch]
    # Heuristic: *_s* solid component surfaces → solid region
    base = re.sub(r"_\d+$", "", patch)
    if base.endswith("_s") or base in ("CU", "Cover", "fin1", "fin2", "impeller2", "case1", "case2"):
        for r in cfg.solid_regions:
            return r
    if "ami" in patch.lower() or patch.startswith("open"):
        for r in cfg.fluid_regions:
            return r
    return None


def generate_case(cfg: CaseConfig, *, dry_run: bool = False) -> dict:
    """Build output case directory and helper scripts."""
    missing = validate_mesh_complete(cfg.source_case)
    if missing:
        raise FileNotFoundError(
            f"Source mesh incomplete (missing: {', '.join(missing)}). "
            "Run cgns2foam conversion first."
        )

    mesh = load_mesh(cfg.source_case)
    zone_names = [z.name for z in mesh.cell_zones]
    for reg in cfg.regions:
        for z in reg.cell_zones:
            if z not in zone_names:
                raise ValueError(
                    f"cellZone '{z}' for region '{reg.name}' not found. "
                    f"Available: {zone_names}"
                )

    patch_region = dict(cfg.patch_regions)
    for p in mesh.patch_names:
        patch_region.setdefault(p, _infer_patch_region(p, cfg))

    interfaces = build_interface_list(
        mesh, cfg.raw, cfg.resolve_region_type, patch_region
    )

    report = {
        "source": str(cfg.source_case),
        "output": str(cfg.output_case),
        "cell_zones": zone_names,
        "interfaces": [
            {
                "master": i.master,
                "slave": i.slave,
                "kind": i.kind.value,
                "method": i.method.value,
            }
            for i in interfaces
        ],
    }

    if dry_run:
        return report

    out = cfg.output_case
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    _copy_mesh(cfg.source_case, out)

    # --- constant (regionProperties written in Allrun.pre before split) ---
    _write(
        out / "system" / "regionProperties",
        region_properties(cfg.fluid_regions, cfg.solid_regions),
    )
    _write(out / "constant" / "g", gravity_vector(cfg.gravity))

    for reg in cfg.regions:
        mat = cfg.material_for(reg.foam_name)
        cdir = out / "constant" / reg.foam_name
        if reg.type == "fluid":
            _write(cdir / "thermophysicalProperties", thermophysical_fluid(mat))
            _write(cdir / "turbulenceProperties", turbulence_properties(cfg.turbulence))
        else:
            _write(cdir / "thermophysicalProperties", thermophysical_solid(mat))

    # --- system ---
    _write(out / "system" / "controlDict", control_dict(cfg.numerics, cfg.solver))
    _write(out / "system" / "fvSchemes", fv_schemes_fluid())
    _write(out / "system" / "fvSolution", fv_solution_fluid(cfg.numerics))

    for reg in cfg.regions:
        sdir = out / "system" / reg.foam_name
        if reg.type == "fluid":
            _write(sdir / "fvSchemes", fv_schemes_fluid())
            _write(sdir / "fvSolution", fv_solution_fluid(cfg.numerics))
        else:
            _write(sdir / "fvSchemes", fv_schemes_solid())
            _write(sdir / "fvSolution", fv_solution_solid())

    # --- 0.orig per region (used after splitMeshRegions) ---
    T0 = cfg.initial.get("T", 300)
    U0 = cfg.initial.get("U", [0, 0, 0])
    p0 = cfg.initial.get("p", 101325)

    for reg in cfg.regions:
        rbc = cfg.boundary_conditions.get(reg.name, cfg.boundary_conditions.get(reg.foam_name, {}))
        patches = mesh.patch_names
        odir = out / "0.orig" / reg.foam_name
        _write(odir / "T", field_T(reg.type, patches, rbc.get("T", {}), T0))
        _write(odir / "p", field_p(patches, p0))
        if reg.type == "fluid":
            _write(odir / "U", field_U(patches, rbc.get("U", {}), U0))
            _write(odir / "p_rgh", field_p_rgh(patches, 0))

    # AMI baffles dict
    ami_pairs = [
        (i.master, i.slave)
        for i in interfaces
        if i.method == InterfaceMethod.CYCLIC_AMI
    ]
    if ami_pairs:
        rot_axis = cfg.interfaces.get("ami_rotation_axis", [0, 0, 1])
        _write(
            out / "system" / "createBafflesDict",
            create_baffles_ami(ami_pairs, rot_axis),
        )

    # --- run scripts ---
    _write(out / "setup_report.json", json.dumps(report, indent=2))
    _write(out / "Allrun.pre", _allrun_pre(cfg, interfaces))
    _write(out / "Allrun", _allrun(cfg))
    _write(out / "Allclean", _allclean())

    return report


def _allrun_pre(cfg: CaseConfig, interfaces) -> str:
    stitch = cfg.mesh_prep.get("stitch_interfaces", True)
    split_opts = list(cfg.mesh_prep.get("split_options", ["-cellZonesOnly", "-overwrite"]))
    combine = cfg.mesh_prep.get("combine_zones", {})
    for reg in cfg.regions:
        if len(reg.cell_zones) > 1:
            combine.setdefault(reg.name, reg.cell_zones)

    for reg_name, zone_list in combine.items():
        zones_str = " ".join(zone_list)
        split_opts.append(f"-combineZones")
        split_opts.append(f"({zones_str})")

    split_cmd = " ".join(split_opts)

    lines = [
        "#!/bin/sh",
        'cd "${0%/*}" || exit',
        ". ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions",
        "#------------------------------------------------------------------------------",
        "# Generated by foam2thermal – mesh prep for chtMultiRegionSimpleFoam",
        "",
    ]

    if stitch:
        for iface in interfaces:
            if iface.method.value != "stitch":
                continue
            lines.append(
                f"runApplication stitchMesh -overwrite {iface.master} {iface.slave}"
            )

    ami_pairs = [i for i in interfaces if i.method.value == "cyclicAMI"]
    if ami_pairs:
        lines.append("runApplication createBaffles -overwrite")

    lines.extend([
        "cp system/regionProperties constant/regionProperties",
        f"runApplication splitMeshRegions {split_cmd}",
        "",
        "# Restore initial fields per region",
        "restore0Dir -allRegions",
        "",
        "# Remove incompatible fields (fluid-only / solid-only)",
    ])

    for reg in cfg.regions:
        if reg.type == "solid":
            for f in ("U", "p_rgh", "k", "epsilon", "nut", "alphat"):
                lines.append(f"rm -f 0/{reg.foam_name}/{f} 2>/dev/null || true")
        else:
            lines.append(f"# fluid region {reg.foam_name}: keep U, p_rgh, T")

    lines.append("")
    lines.append("#------------------------------------------------------------------------------")
    return "\n".join(lines) + "\n"


def _allrun(cfg: CaseConfig) -> str:
    return "\n".join([
        "#!/bin/sh",
        'cd "${0%/*}" || exit',
        ". ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions",
        "#------------------------------------------------------------------------------",
        "./Allrun.pre",
        f"runApplication $(getApplication)",
        "#------------------------------------------------------------------------------",
        "",
    ])


def _allclean() -> str:
    return "\n".join([
        "#!/bin/sh",
        'cd "${0%/*}" || exit',
        ". ${WM_PROJECT_DIR:?}/bin/tools/CleanFunctions",
        "cleanCase",
        "#------------------------------------------------------------------------------",
        "",
    ])
