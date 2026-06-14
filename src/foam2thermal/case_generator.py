"""Generate a complete chtMultiRegionSimpleFoam case from JSON config."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .config import CaseConfig
from .interfaces import InterfaceMethod, build_interface_list
from .mesh import load_mesh, repair_cell_zones, validate_mesh_complete
from .mesh_coalesce import coalesce_zone_interfaces
from .templates import (
    control_dict,
    create_patch_ami,
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
    stitch_mesh_dict,
    thermophysical_fluid,
    thermophysical_solid,
    tolerance_dict,
    topo_set_cell_zones,
    turbulence_properties,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _copy_mesh(source: Path, dest: Path, mesh_prep: dict) -> dict:
    src_poly = source / "constant" / "polyMesh"
    dst_poly = dest / "constant" / "polyMesh"
    if dst_poly.exists():
        shutil.rmtree(dst_poly)
    shutil.copytree(src_poly, dst_poly)
    repair_cell_zones(dst_poly)
    coalesce_report: dict = {"paired_faces": 0}
    if mesh_prep.get("coalesce_interfaces", True):
        tol = float(mesh_prep.get("coalesce_point_tol", 1e-4))
        coalesce_report = coalesce_zone_interfaces(
            dst_poly,
            point_tol=tol,
            exclude_patterns=mesh_prep.get("coalesce_exclude_patterns", [r"ami_rot"]),
        )
    return coalesce_report


def _copy_system_for_prep(source: Path, dest: Path) -> None:
    """Copy input mesh system/* for monolithic-mesh utilities (stitchMesh, split)."""
    src_sys = source / "system"
    dst_sys = dest / "system"
    dst_sys.mkdir(parents=True, exist_ok=True)
    for name in ("controlDict", "fvSchemes", "fvSolution"):
        src = src_sys / name
        if src.is_file():
            shutil.copy2(src, dst_sys / name)


def _infer_patch_region(patch: str, cfg: CaseConfig) -> str | None:
    if patch in cfg.patch_regions:
        return cfg.patch_regions[patch]
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

    coalesce_report = _copy_mesh(cfg.source_case, out, cfg.mesh_prep)
    report["mesh_coalesce"] = coalesce_report
    mesh = load_mesh(out)
    _copy_system_for_prep(cfg.source_case, out)

    # --- staged region configs (deployed after splitMeshRegions) ---
    _write(out / "constant" / "g", gravity_vector(cfg.gravity))
    _write(
        out / "system" / "regionProperties",
        region_properties(cfg.fluid_regions, cfg.solid_regions),
    )
    _write(
        out / "system" / "controlDict.cht",
        control_dict(cfg.numerics, cfg.solver),
    )

    for reg in cfg.regions:
        mat = cfg.material_for(reg.foam_name)
        cdir = out / "constant.orig" / reg.foam_name
        if reg.type == "fluid":
            _write(cdir / "thermophysicalProperties", thermophysical_fluid(mat))
            _write(cdir / "turbulenceProperties", turbulence_properties(cfg.turbulence))
        else:
            _write(cdir / "thermophysicalProperties", thermophysical_solid(mat))

        sdir = out / "system.orig" / reg.foam_name
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

    ami_pairs = [
        (i.master, i.slave)
        for i in interfaces
        if i.method == InterfaceMethod.CYCLIC_AMI
    ]
    ami_on_mesh = [
        (m, s) for m, s in ami_pairs
        if m in mesh.patch_names and s in mesh.patch_names
    ]
    if ami_on_mesh:
        rot_axis = cfg.interfaces.get("ami_rotation_axis", [0, 0, 1])
        _write(
            out / "system" / "createPatchDict",
            create_patch_ami(ami_on_mesh, rot_axis),
        )

    tol = cfg.mesh_prep.get("stitch_tolerance", {})
    _write(
        out / "system" / "toleranceDict",
        tolerance_dict(
            float(tol.get("pointMergeTol", 0.1)),
            float(tol.get("edgeMergeTol", 0.05)),
        ),
    )

    stitch_entries = _stitch_dict_entries(interfaces, mesh, cfg.mesh_prep)
    if stitch_entries:
        _write(out / "system" / "stitchMeshDict", stitch_mesh_dict(stitch_entries))

    _write(
        out / "system" / "topoSetDict",
        topo_set_cell_zones(zone_names),
    )

    scripts_src = Path(__file__).resolve().parents[2] / "scripts"
    scripts_dst = out / "scripts"
    scripts_dst.mkdir(parents=True, exist_ok=True)
    for name in ("relocateRegionMeshes.sh", "verifyRegions.sh", "split_regions.py"):
        src = scripts_src / name
        if src.is_file():
            shutil.copy2(src, scripts_dst / name)

    _write(out / "setup_report.json", json.dumps(report, indent=2))
    _write(out / "Allrun.pre", _allrun_pre(cfg, interfaces, mesh, ami_on_mesh))
    _write(out / "Allrun", _allrun(cfg))
    _write(out / "Allclean", _allclean())

    return report


def _stitch_command(
    master: str,
    slave: str,
    mesh,
    mesh_prep: dict,
) -> str | None:
    """Return runApplication stitchMesh line, or None to skip."""
    by_name = {p.name: p for p in mesh.patches}
    if master not in by_name or slave not in by_name:
        return None
    nf_m = by_name[master].n_faces
    nf_s = by_name[slave].n_faces
    ratio = max(nf_m, nf_s) / max(min(nf_m, nf_s), 1)
    max_integral = mesh_prep.get("stitch_integral_ratio_max", 1.05)
    max_partial = mesh_prep.get("stitch_partial_ratio_max", 3.0)
    stitch_mode = mesh_prep.get("stitch_mode", "partial")

    if ratio > max_partial:
        return f"# SKIP stitch {master}/{slave}: face ratio {ratio:.2f} > {max_partial}"

    tag = _safe_stitch_name(master, slave)
    if stitch_mode == "partial":
        mode = "-partial"
    elif stitch_mode == "integral":
        mode = ""
    else:  # auto
        mode = "-partial" if ratio > max_integral else ""
    parts = ["runApplication", f"-s stitch_{tag}", "stitchMesh"]
    if mode:
        parts.append(mode)
    parts.extend(["-overwrite", "-toleranceDict", "system/toleranceDict", master, slave])
    return " ".join(parts)


def _stitch_dict_entries(interfaces, mesh, mesh_prep: dict) -> list[tuple[str, str, str, str]]:
    """Build stitchMeshDict entries: (key, master, slave, match mode)."""
    if not mesh_prep.get("stitch_interfaces", False):
        return []
    by_name = {p.name: p for p in mesh.patches}
    max_integral = mesh_prep.get("stitch_integral_ratio_max", 1.05)
    max_partial = mesh_prep.get("stitch_partial_ratio_max", 3.0)
    stitch_mode = mesh_prep.get("stitch_mode", "partial")
    entries: list[tuple[str, str, str, str]] = []
    for iface in interfaces:
        if iface.method.value != "stitch":
            continue
        master, slave = iface.master, iface.slave
        if master not in by_name or slave not in by_name:
            continue
        nf_m = by_name[master].n_faces
        nf_s = by_name[slave].n_faces
        ratio = max(nf_m, nf_s) / max(min(nf_m, nf_s), 1)
        if ratio > max_partial:
            continue
        if stitch_mode == "partial":
            mode = "partial"
        elif stitch_mode == "integral":
            mode = "integral"
        else:
            mode = "partial" if ratio > max_integral else "integral"
        key = _safe_stitch_name(master, slave)
        entries.append((key, master, slave, mode))
    return entries


def _safe_stitch_name(master: str, slave: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", f"{master}_{slave}")[:48]


def _allrun_pre(cfg: CaseConfig, interfaces, mesh, ami_on_mesh: list[tuple[str, str]]) -> str:
    mesh_prep = cfg.mesh_prep
    stitch = mesh_prep.get("stitch_interfaces", False)
    split_opts = list(mesh_prep.get("split_options", ["-cellZonesOnly", "-overwrite"]))
    combine = dict(mesh_prep.get("combine_zones", {}))
    for reg in cfg.regions:
        if len(reg.cell_zones) > 1:
            combine.setdefault(reg.name, reg.cell_zones)

    for _reg_name, zone_list in combine.items():
        zones_str = " ".join(zone_list)
        split_opts.extend(["-combineZones", f"({zones_str})"])

    split_cmd = " ".join(split_opts)
    region_names = " ".join(reg.foam_name for reg in cfg.regions)

    lines = [
        "#!/bin/sh",
        "set -e",
        "export FOAM_SIGFPE=0 FOAM_SETNAN=0",
        'cd "${0%/*}" || exit',
        ". ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions",
        "#------------------------------------------------------------------------------",
        "# foam2thermal – mesh prep (monolithic mesh, no constant/<region> yet)",
        "",
    ]

    if stitch:
        lines.extend([
            "# Optional stitch for remaining unmatched interface patches",
            "set +e",
            "STITCH_FAIL=0",
        ])
        for iface in interfaces:
            if iface.method.value != "stitch":
                continue
            cmd = _stitch_command(iface.master, iface.slave, mesh, mesh_prep)
            if cmd and not cmd.startswith("# SKIP"):
                lines.append(f"{cmd} || STITCH_FAIL=$((STITCH_FAIL + 1))")
            elif cmd:
                lines.append(cmd)
        lines.extend([
            "set -e",
            'if [ "$STITCH_FAIL" -gt 0 ]; then',
            '    echo "WARNING: $STITCH_FAIL stitchMesh pair(s) failed – see log.stitchMesh.*"',
            "fi",
            "",
        ])

    if ami_on_mesh and mesh_prep.get("create_ami_patches", False):
        lines.append("runApplication -s createPatch createPatch -overwrite")

    lines.extend([
        "set +e",
        "runApplication -s checkMesh checkMesh -noTopology",
        "CM=$?",
        "set -e",
        'if [ "$CM" -ne 0 ]; then echo "WARNING: checkMesh exit $CM (see log.checkMesh.checkMesh)"; fi',
        "runApplication -s topoSet topoSet",
        "cp system/regionProperties constant/regionProperties",
        "# splitMeshRegions crashes on Windows MinGW when writing regional meshes;",
        "# use foam2thermal Python splitter (same topology, CHT _to_ patches).",
        'FOAM2THERMAL_ROOT="$(cd "${0%/*}/../.." && pwd)"',
        'export PYTHONPATH="${FOAM2THERMAL_ROOT}/src:${PYTHONPATH}"',
        'python "${0%/*}/scripts/split_regions.py" "$(pwd)"',
        "sh scripts/verifyRegions.sh",
        "",
        "# Deploy per-region constant/system and CHT controlDict",
        f"for region in {region_names}; do",
        '    mkdir -p "constant/${region}" "system/${region}"',
        '    cp -f constant.orig/"${region}"/* "constant/${region}/" 2>/dev/null || true',
        '    cp -f system.orig/"${region}"/* "system/${region}/" 2>/dev/null || true',
        "done",
        "cp -f system/controlDict.cht system/controlDict",
        "runApplication -s renumberMesh renumberMesh -allRegions -overwrite",
        "",
        "restore0Dir -allRegions",
        "",
        "# Remove incompatible fields (fluid-only / solid-only)",
    ])

    for reg in cfg.regions:
        if reg.type == "solid":
            for f in ("U", "p_rgh", "k", "epsilon", "nut", "alphat"):
                lines.append(f"rm -f 0/{reg.foam_name}/{f} 2>/dev/null || true")

    lines.append("")
    lines.append("#------------------------------------------------------------------------------")
    return "\n".join(lines) + "\n"


def _allrun(cfg: CaseConfig) -> str:
    return "\n".join([
        "#!/bin/sh",
        "set -e",
        'cd "${0%/*}" || exit',
        ". ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions",
        "#------------------------------------------------------------------------------",
        "./Allrun.pre",
        "runApplication $(getApplication)",
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
