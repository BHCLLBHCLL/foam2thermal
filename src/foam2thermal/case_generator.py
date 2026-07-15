"""Generate a complete chtMultiRegionSimpleFoam case from JSON config."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .config import CaseConfig
from .interfaces import InterfaceMethod, build_interface_list
from .mesh import (
    build_patch_region_map,
    load_mesh,
    repair_cell_zones,
    validate_mesh_complete,
    zone_bbox_centroid,
)
from .mesh_coalesce import coalesce_zone_interfaces, _write_binary_label_list
from .mesh_split import field_patches_for_region, interface_neighbors
from .paths import win_to_msys
from .templates import (
    build_region_fv_options,
    control_dict,
    create_patch_ami,
    decompose_par_dict,
    field_alphat,
    field_epsilon,
    field_k,
    field_nut,
    field_p,
    field_p_rgh,
    field_T,
    field_U,
    fv_schemes_fluid,
    fv_schemes_solid,
    fv_solution_fluid,
    fv_solution_solid,
    gravity_vector,
    mrf_properties,
    radiation_properties,
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


def _cell_to_region_header() -> bytes:
    return (
        b"FoamFile\n{\n    version 2.0;\n    format binary;\n"
        b"    class labelList;\n    object cellToRegion;\n}\n\n"
    )


def _write_cell_to_region(out: Path, mesh, regions: list) -> None:
    """Map each cell to a split-region index (fluid regions first, then solid)."""
    import numpy as np

    fluid = [r for r in regions if r.type == "fluid"]
    solid = [r for r in regions if r.type == "solid"]
    ordered = fluid + solid
    zone_map: dict[str, int] = {}
    for ri, reg in enumerate(ordered):
        for z in reg.cell_zones:
            zone_map[z] = ri

    n_cells = max((max(z.cell_labels) for z in mesh.cell_zones if z.cell_labels), default=-1) + 1
    cell_region = np.full(n_cells, -1, dtype=np.int32)
    for z in mesh.cell_zones:
        if z.name not in zone_map:
            continue
        ri = zone_map[z.name]
        labels = np.asarray(z.cell_labels, dtype=np.int64)
        valid = (labels >= 0) & (labels < n_cells)
        cell_region[labels[valid]] = ri
    if np.any(cell_region < 0):
        missing = int(np.sum(cell_region < 0))
        raise ValueError(f"{missing} cell(s) not assigned to any region in cellToRegion")

    ctr = out / "constant" / "cellToRegion"
    _write_binary_label_list(ctr, cell_region, _cell_to_region_header())


def _ami_patterns(cfg: CaseConfig) -> list[str]:
    return cfg.interfaces.get("ami_patterns", [r"ami_rot\d+"])


def _mrf_non_rotating_patches(
    patches: list[str],
    ami_patterns: list[str],
    rotating_zones: list[str],
) -> list[str]:
    """Patches on the rotating cellZone that should NOT rotate.

    For a single MRF zone this is the set of AMI patches that belong to
    *this* rotating zone plus any external boundary patches (open*).
    Coupling patches (``*_to_*``) are added after split by the solver.
    """
    from .interfaces import is_ami_patch

    out: list[str] = []
    for p in patches:
        if is_ami_patch(p, ami_patterns):
            out.append(p)
        elif p == "open" or (p.startswith("open") and p.endswith("_1")):
            out.append(p)
        elif "_to_" in p:
            out.append(p)
    return sorted(set(out))


def _is_ras(cfg: CaseConfig) -> bool:
    """True when the configured turbulence model needs k/epsilon/nut/alphat fields."""
    sim = str(cfg.turbulence.get("simulationType", "laminar")).lower()
    return sim not in ("laminar", "")


def _radiation_model_for(cfg: CaseConfig, region_name: str, foam_name: str) -> str:
    """Resolve the radiationModel name for a region (default 'none').

    Config hook (all optional)::

        "radiation": "none"                      # global model name, or
        "radiation": { "default": "none",        # global default + overrides
                       "air": "fvDOM" }
    """
    rad = cfg.raw.get("radiation")
    if isinstance(rad, str):
        return rad
    if isinstance(rad, dict):
        for key in (region_name, foam_name):
            if key in rad:
                return str(rad[key])
        if "default" in rad:
            return str(rad["default"])
    return "none"


def _default_mrf_axis_for_zone(zone_name: str) -> list[float]:
    """Default MRF rotation axis from cellZone name (cgns2foam FPHPARTS.rotation*).

    Symmetric dual-fan layouts should rotate in the same direction so their
    axial flows add rather than cancel.  Both rotation1/rotation2 default to
    +Y; override via ``mrf.axes`` in config if opposite rotation is intended.
    """
    name = zone_name.lower()
    if "rotation1" in name or "rotation2" in name:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


def _mrf_axes(rot_zones: list[str], mrf: dict) -> list[list[float]]:
    """Resolve per-zone MRF axes (mrf.axes dict/list, mrf.axis, or name defaults)."""
    if "axes" in mrf:
        cfg = mrf["axes"]
        if isinstance(cfg, dict):
            return [
                [float(v) for v in cfg.get(z, _default_mrf_axis_for_zone(z))]
                for z in rot_zones
            ]
        if isinstance(cfg, list) and len(cfg) == len(rot_zones):
            return [[float(c) for c in ax] for ax in cfg]
    if "axis" in mrf:
        ax = [float(v) for v in mrf["axis"]]
        return [ax] * len(rot_zones)
    return [_default_mrf_axis_for_zone(z) for z in rot_zones]


def _copy_mesh(source: Path, dest: Path, mesh_prep: dict) -> dict:
    import sys

    def _log(msg: str) -> None:
        print(f"[copy_mesh] {msg}", file=sys.stderr, flush=True)

    src_poly = source / "constant" / "polyMesh"
    dst_poly = dest / "constant" / "polyMesh"
    _log("rmtree existing polyMesh ...")
    if dst_poly.exists():
        shutil.rmtree(dst_poly)
    _log("copytree source polyMesh ...")
    shutil.copytree(src_poly, dst_poly)
    _log("repair_cell_zones ...")
    repair_cell_zones(dst_poly)
    coalesce_report: dict = {"paired_faces": 0}
    if mesh_prep.get("coalesce_interfaces", True):
        tol = float(mesh_prep.get("coalesce_point_tol", 1e-4))
        geom_tol = mesh_prep.get("coalesce_geom_tol")
        _log("coalesce_zone_interfaces begin ...")
        coalesce_report = coalesce_zone_interfaces(
            dst_poly,
            point_tol=tol,
            exclude_patterns=mesh_prep.get("coalesce_exclude_patterns", [r"ami_rot"]),
            geometric_fallback=mesh_prep.get("coalesce_geometric_fallback", True),
            geom_tol=float(geom_tol) if geom_tol is not None else None,
        )
        _log(f"coalesce_zone_interfaces done: {coalesce_report.get('paired_faces', 0)} paired")
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


def cell_zone_to_config_region(cfg: CaseConfig) -> dict[str, str]:
    """Map cellZone names from the mesh to JSON config region names."""
    mapping: dict[str, str] = {}
    for reg in cfg.regions:
        for z in reg.cell_zones:
            mapping[z] = reg.name
    return mapping


def _infer_patch_region(patch: str, cfg: CaseConfig) -> str | None:
    """Infer which configured region a monolithic-mesh patch belongs to.

    cgns2foam emits patches named ``<bc>`` / ``<bc>_1`` / ``<bc>_2`` …
    for the same BC appearing in successive CGNS zones.  We strip the
    trailing ``_<digit>`` suffix to get the base BC name, then map the
    base name to a region using heuristics:
      - ``ami_rot*`` / ``open*`` / ``impeller*`` → air (fluid)
      - ``case1*`` → case1 (fluid)
      - ``case2*`` → case2 (fluid)
      - ``CU*`` / ``Cover*`` / ``fin1*`` / ``fin2*`` → matching solid
    """
    if patch in cfg.patch_regions:
        return cfg.patch_regions[patch]

    # Strip trailing _<digit> suffixes (e.g. case1_s_2 -> case1_s)
    base = re.sub(r"_\d+$", "", patch)

    # AMI / open / impeller → air fluid region
    if "ami" in base.lower() or base.startswith("open") or base.startswith("impeller"):
        for r in cfg.fluid_regions:
            if r == "air":
                return r
        return cfg.fluid_regions[0] if cfg.fluid_regions else None

    # Match configured region names (fluid or solid) by patch name prefix.
    # case1/case2 may be solid (regionProperties) or fluid depending on config.
    for r in list(cfg.fluid_regions) + list(cfg.solid_regions):
        if base.lower().startswith(r.lower()):
            return r

    return None


def generate_case(cfg: CaseConfig, *, dry_run: bool = False) -> dict:
    """Build output case directory and helper scripts."""
    import sys

    def _log(msg: str) -> None:
        print(f"[build] {msg}", file=sys.stderr, flush=True)

    _log("validate source mesh ...")
    missing = validate_mesh_complete(cfg.source_case)
    if missing:
        raise FileNotFoundError(
            f"Source mesh incomplete (missing: {', '.join(missing)}). "
            "Run cgns2foam conversion first."
        )

    import time
    _log("load source mesh ...")
    t0 = time.time()
    mesh = load_mesh(cfg.source_case)
    _log(f"  loaded: {len(mesh.patches)} patches, {len(mesh.cell_zones)} zones ({time.time()-t0:.1f}s)")
    zone_names = [z.name for z in mesh.cell_zones]
    for reg in cfg.regions:
        for z in reg.cell_zones:
            if z not in zone_names:
                raise ValueError(
                    f"cellZone '{z}' for region '{reg.name}' not found. "
                    f"Available: {zone_names}"
                )

    patch_region = build_patch_region_map(
        cfg.source_case,
        mesh,
        explicit=cfg.patch_regions,
        cell_zone_to_region=cell_zone_to_config_region(cfg) or None,
        name_heuristic=lambda p: _infer_patch_region(p, cfg),
    )

    _log("build interface list ...")
    t0 = time.time()
    interfaces = build_interface_list(
        mesh, cfg.raw, cfg.resolve_region_type, patch_region
    )
    _log(f"  {len(interfaces)} interfaces ({time.time()-t0:.1f}s)")

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
    _log(f"remove old output case ({out}) ...")
    t0 = time.time()
    if out.exists():
        shutil.rmtree(out)
    _log(f"  removed ({time.time()-t0:.1f}s)")
    out.mkdir(parents=True)

    coalesce_report = _copy_mesh(cfg.source_case, out, cfg.mesh_prep)
    report["mesh_coalesce"] = coalesce_report
    _log(f"coalesce done: {coalesce_report.get('paired_faces', 0)} paired")
    mesh = load_mesh(out)
    _log("write cellToRegion ...")
    _write_cell_to_region(out, mesh, cfg.regions)
    _log("copy system files ...")
    _copy_system_for_prep(cfg.source_case, out)
    _write(
        out / "system" / "regionProperties",
        region_properties(cfg.fluid_regions, cfg.solid_regions),
    )
    _log("compute interface neighbors ...")
    region_neighbors = interface_neighbors(out)
    _log(f"neighbors: {region_neighbors}")
    ami_pats = _ami_patterns(cfg)
    T0 = cfg.initial.get("T", 300)
    U0 = cfg.initial.get("U", [0, 0, 0])
    p0 = cfg.initial.get("p", 101325)

    # --- staged region configs (deployed after splitMeshRegions) ---
    _write(out / "constant" / "g", gravity_vector(cfg.gravity))
    _write(
        out / "system" / "controlDict.cht",
        control_dict(cfg.numerics, cfg.solver),
    )
    _write(
        out / "system" / "decomposeParDict",
        decompose_par_dict(cfg.n_procs),
    )

    ras = _is_ras(cfg)
    k0 = cfg.initial.get("k", 0.1)
    eps0 = cfg.initial.get("epsilon", 0.01)
    patch_types = {p.name: p.patch_type for p in mesh.patches}

    for reg in cfg.regions:
        _log(f"region {reg.name} ({reg.type}) ...")
        mat = cfg.material_for(reg.foam_name)
        cdir = out / "constant.orig" / reg.foam_name
        _write(
            cdir / "radiationProperties",
            radiation_properties(_radiation_model_for(cfg, reg.name, reg.foam_name)),
        )
        if reg.type == "fluid":
            _write(cdir / "thermophysicalProperties", thermophysical_fluid(mat))
            _write(cdir / "turbulenceProperties", turbulence_properties(cfg.turbulence))
            mrf = next((r.get("mrf") for r in cfg.raw["regions"] if r["name"] == reg.name), None)
            if mrf:
                rot_zones = mrf.get("cellZones", [])
                axes = _mrf_axes(rot_zones, mrf)
                omega = float(mrf.get("omega", 100))
                origin_spec = mrf.get("origin", "centroid")
                if origin_spec == "centroid":
                    if len(rot_zones) == 1:
                        _log(f"  MRF centroid for {rot_zones} ...")
                        origins = [
                            zone_bbox_centroid(out / "constant" / "polyMesh", rot_zones)
                        ]
                    else:
                        origins = [
                            zone_bbox_centroid(out / "constant" / "polyMesh", [z])
                            for z in rot_zones
                        ]
                    _log(f"  MRF origins: {origins}")
                else:
                    pt = tuple(float(v) for v in origin_spec)
                    origins = [pt] * len(rot_zones)
                nr = mrf.get("nonRotatingPatches")
                if nr is None:
                    nr = _mrf_non_rotating_patches(mesh.patch_names, ami_pats, rot_zones)
                _write(
                    cdir / "MRFProperties",
                    mrf_properties(rot_zones, origins, axes, omega, nr),
                )
        else:
            _write(cdir / "thermophysicalProperties", thermophysical_solid(mat))

        sdir = out / "system.orig" / reg.foam_name
        _write(sdir / "decomposeParDict", decompose_par_dict(cfg.n_procs, location="system"))
        if reg.type == "fluid":
            _write(sdir / "fvSchemes", fv_schemes_fluid())
            _write(sdir / "fvSolution", fv_solution_fluid(cfg.numerics, p_ref=p0))
        else:
            _write(sdir / "fvSchemes", fv_schemes_solid())
            _write(sdir / "fvSolution", fv_solution_solid())
        fv_opt = build_region_fv_options(
            region_type=reg.type,
            region_name=reg.name,
            boundary_conditions=cfg.boundary_conditions,
            numerics=cfg.numerics,
        )
        if fv_opt:
            _write(sdir / "fvOptions", fv_opt)

    _log("write 0.orig fields ...")
    for reg in cfg.regions:
        rbc = cfg.boundary_conditions.get(reg.name, cfg.boundary_conditions.get(reg.foam_name, {}))
        patches = field_patches_for_region(
            reg.foam_name,
            config_name=reg.name,
            monolithic_patch_names=mesh.patch_names,
            patch_region=patch_region,
            neighbors=region_neighbors,
        )
        odir = out / "0.orig" / reg.foam_name
        _write(odir / "T", field_T(reg.type, patches, rbc.get("T", {}), T0, ami_patterns=ami_pats))
        _write(odir / "p", field_p(patches, p0, ami_patterns=ami_pats))
        if reg.type == "fluid":
            _write(odir / "U", field_U(patches, rbc.get("U", {}), U0, ami_patterns=ami_pats))
            _write(odir / "p_rgh", field_p_rgh(patches, 0, bc_cfg=rbc.get("p_rgh", {}), ami_patterns=ami_pats))
            if ras:
                _write(odir / "k", field_k(patches, rbc.get("k", {}), k0, ami_patterns=ami_pats, patch_types=patch_types))
                _write(odir / "epsilon", field_epsilon(patches, rbc.get("epsilon", {}), eps0, ami_patterns=ami_pats, patch_types=patch_types))
                _write(odir / "nut", field_nut(patches, rbc.get("nut", {}), ami_patterns=ami_pats, patch_types=patch_types))
                _write(odir / "alphat", field_alphat(patches, rbc.get("alphat", {}), ami_patterns=ami_pats, patch_types=patch_types))

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
    for name in (
        "relocateRegionMeshes.sh",
        "verifyRegions.sh",
        "split_regions.py",
        "fix_mapped_wall_patches.py",
        "fix_cyclic_ami_patches.py",
        "sync_region_fields.py",
    ):
        src = scripts_src / name
        if src.is_file():
            shutil.copy2(src, scripts_dst / name)

    _log("write scripts and reports ...")
    _write(out / "setup_report.json", json.dumps(report, indent=2))
    meta = cfg.raw.setdefault("_meta", {})
    meta["source_mesh"] = str(cfg.source_case)
    # Persist topology-inferred patch→region and scanned interfaces so that
    # split_regions / fix scripts pick up mappedWall + AMI pairs (BCs_fix etc.).
    cfg.raw["patch_regions"] = {k: v for k, v in patch_region.items() if v}
    iface_cfg = cfg.raw.setdefault("interfaces", {})
    scanned_explicit = [
        {
            "master": i.master,
            "slave": i.slave,
            "method": i.method.value,
            "kind": i.kind.value,
        }
        for i in interfaces
    ]
    # Keep user-provided explicit first; append scanned pairs not already listed.
    user_explicit = list(iface_cfg.get("explicit", []))
    seen_pairs = {(e["master"], e["slave"]) for e in user_explicit}
    seen_pairs |= {(e["slave"], e["master"]) for e in user_explicit}
    for item in scanned_explicit:
        key = (item["master"], item["slave"])
        if key in seen_pairs or (item["slave"], item["master"]) in seen_pairs:
            continue
        user_explicit.append(item)
        seen_pairs.add(key)
    iface_cfg["explicit"] = user_explicit
    _write(out / "config.json", json.dumps(cfg.raw, indent=2, ensure_ascii=False))
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
        f'PYTHON="{win_to_msys(cfg.python_exe)}"',
        "export PYTHON",
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

    first_region = cfg.regions[0].foam_name if cfg.regions else "region0"

    lines.extend([
        "# Monolithic-mesh steps (skip when regions already split)",
        "if [ -f constant/polyMesh/boundary ]; then",
        "    set +e",
        "    runApplication -s checkMesh checkMesh -noTopology",
        "    CM=$?",
        "    set -e",
        '    if [ "$CM" -ne 0 ]; then echo "WARNING: checkMesh exit $CM (see log.checkMesh.checkMesh)"; fi',
        "    if [ -f constant/cellToRegion ]; then",
        '        echo "Skipping topoSet: constant/cellToRegion present (from build)"',
        "    else",
        "        runApplication -s topoSet topoSet",
        "    fi",
        "    cp system/regionProperties constant/regionProperties",
    ])

    if ami_on_mesh and mesh_prep.get("create_ami_patches", True):
        lines.extend([
            "    # createPatch -overwrite may drop cellZones; back up & restore",
            "    if [ -f constant/polyMesh/cellZones ]; then",
            "        cp -f constant/polyMesh/cellZones constant/polyMesh/cellZones.bak",
            "    fi",
            "    set +e",
            "    runApplication -s createPatch createPatch -overwrite",
            "    CP=$?",
            "    set -e",
            "    if [ -f constant/polyMesh/cellZones.bak ]; then",
            "        cp -f constant/polyMesh/cellZones.bak constant/polyMesh/cellZones",
            "        rm -f constant/polyMesh/cellZones.bak",
            "    fi",
            '    if [ "$CP" -ne 0 ]; then',
            '        echo "WARNING: createPatch exit $CP (see log.createPatch.createPatch); fixing AMI after split"',
            "    fi",
        ])

    lines.extend([
        "    # splitMeshRegions crashes on Windows MinGW – use Python splitter.",
        '    FOAM2THERMAL_ROOT="$(cd "${0%/*}/../.." && pwd)"',
        '    export PYTHONPATH="${FOAM2THERMAL_ROOT}/src:${PYTHONPATH}"',
        '    "$PYTHON" "${0%/*}/scripts/split_regions.py" "$(pwd)"',
        "else",
        f'    if [ ! -f "constant/{first_region}/polyMesh/points" ]; then',
        '        echo "ERROR: no monolithic or regional polyMesh – run setup_cht_case.py build first"',
        "        exit 1",
        "    fi",
        '    echo "Skipping monolithic prep: constant/polyMesh absent (regions already split)"',
        '    FOAM2THERMAL_ROOT="$(cd "${0%/*}/../.." && pwd)"',
        '    export PYTHONPATH="${FOAM2THERMAL_ROOT}/src:${PYTHONPATH}"',
        "fi",
        '"$PYTHON" "${0%/*}/scripts/fix_cyclic_ami_patches.py" "$(pwd)"',
        '"$PYTHON" "${0%/*}/scripts/fix_mapped_wall_patches.py" "$(pwd)"',
        "sh scripts/verifyRegions.sh",
        "",
        "# Deploy per-region constant/system and CHT controlDict",
        "# Set FOAM2THERMAL_KEEP_SETTINGS=1 to preserve user-modified system/constant",
        "# settings (e.g. tuned fvSolution, fvOptions) instead of overwriting from .orig.",
        'KEEP_SETTINGS="${FOAM2THERMAL_KEEP_SETTINGS:-0}"',
        f"for region in {region_names}; do",
        '    mkdir -p "constant/${region}" "system/${region}"',
        '    if [ "$KEEP_SETTINGS" != "1" ]; then',
        '        cp -f constant.orig/"${region}"/* "constant/${region}/" 2>/dev/null || true',
        '        cp -f system.orig/"${region}"/* "system/${region}/" 2>/dev/null || true',
        "    fi",
        "done",
        'if [ "$KEEP_SETTINGS" != "1" ]; then',
        "    cp -f system/controlDict.cht system/controlDict",
        "else",
        '    echo "FOAM2THERMAL_KEEP_SETTINGS=1: preserving existing system/constant settings"',
        "fi",
        '"$PYTHON" "${0%/*}/scripts/sync_region_fields.py" "$(pwd)"',
        "set +e",
        "runApplication -s renumberMesh renumberMesh -allRegions -overwrite",
        "RN=$?",
        "set -e",
        'if [ "$RN" -ne 0 ]; then echo "WARNING: renumberMesh exit $RN (see log.renumberMesh.renumberMesh)"; fi',
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
    n_procs = cfg.n_procs
    return "\n".join([
        "#!/bin/sh",
        "set -e",
        'cd "${0%/*}" || exit',
        ". ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions",
        "#------------------------------------------------------------------------------",
        "./Allrun.pre",
        "",
        "# Decompose and run in parallel",
        f"runApplication -o -s decomposePar decomposePar -allRegions -copyZero -force",
        f"runParallel -o -np {n_procs} $(getApplication)",
        "",
        "# Merge processor* data back to case root (all regions)",
        "runApplication -o -s reconstructParMesh reconstructParMesh -allRegions -constant",
        "runApplication -o -s reconstructPar reconstructPar -allRegions",
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
