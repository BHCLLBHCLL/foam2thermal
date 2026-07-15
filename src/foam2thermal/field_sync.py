"""Sync 0.orig fields with regional polyMesh boundary patches after split."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import load_config
from .interfaces import is_ami_patch
from .mesh import parse_boundary
from .templates import (
    build_region_fv_options,
    field_alphat,
    field_epsilon,
    field_k,
    field_nut,
    field_p,
    field_p_rgh,
    field_T,
    field_U,
)


def _effective_ami_patterns(cfg, parsed_patches) -> list[str]:
    """Patterns covering config AMI names + post-split cyclicAMI patch types."""
    pats = list(cfg.interfaces.get("ami_patterns", [r"ami_rot\d+", r".*[Rr]otation\d*"]))
    names: set[str] = set()
    for e in cfg.interfaces.get("explicit", []):
        if e.get("method") == "cyclicAMI":
            names.add(e["master"])
            names.add(e["slave"])
    for p in parsed_patches:
        if p.patch_type == "cyclicAMI" or is_ami_patch(p.name, pats):
            names.add(p.name)
    # Exact-name patterns so both AMI pair sides get cyclicAMI BCs
    # (e.g. _PartSurface_air_domain_7 does not match *rotation*).
    for n in sorted(names):
        pats.append(re.escape(n))
    return pats


def sync_region_fields(case_dir: Path) -> dict:
    """Rewrite 0.orig/<region>/* using actual post-split boundary patch lists."""
    case_dir = case_dir.resolve()
    cfg_path = case_dir / "config.json"
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Missing {cfg_path} – rebuild case with foam2thermal")

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    meta = raw.get("_meta", {})
    source = Path(meta.get("source_mesh", case_dir))
    cfg = load_config(cfg_path, source, case_dir)

    T0 = cfg.initial.get("T", 300)
    U0 = cfg.initial.get("U", [0, 0, 0])
    p0 = cfg.initial.get("p", 101325)
    k0 = cfg.initial.get("k", 0.1)
    eps0 = cfg.initial.get("epsilon", 0.01)
    ras = str(cfg.turbulence.get("simulationType", "laminar")).lower() not in ("laminar", "")

    by_foam = {r.foam_name: r for r in cfg.regions}
    report: dict[str, list[str]] = {}

    for bnd_path in sorted(case_dir.glob("constant/*/polyMesh/boundary")):
        region = bnd_path.parent.parent.name
        reg = by_foam.get(region)
        if not reg:
            continue
        parsed = parse_boundary(bnd_path)
        patches = [p.name for p in parsed]
        patch_types = {p.name: p.patch_type for p in parsed}
        ami_pats = _effective_ami_patterns(cfg, parsed)
        report[region] = patches

        rbc = cfg.boundary_conditions.get(reg.name, cfg.boundary_conditions.get(reg.foam_name, {}))
        odir = case_dir / "0.orig" / region
        odir.mkdir(parents=True, exist_ok=True)

        (odir / "T").write_text(
            field_T(reg.type, patches, rbc.get("T", {}), T0, ami_patterns=ami_pats),
            encoding="utf-8",
            newline="\n",
        )
        (odir / "p").write_text(
            field_p(patches, p0, ami_patterns=ami_pats),
            encoding="utf-8",
            newline="\n",
        )
        if reg.type == "fluid":
            (odir / "U").write_text(
                field_U(patches, rbc.get("U", {}), U0, ami_patterns=ami_pats),
                encoding="utf-8",
                newline="\n",
            )
            (odir / "p_rgh").write_text(
                field_p_rgh(patches, p0, bc_cfg=rbc.get("p_rgh", {}), ami_patterns=ami_pats),
                encoding="utf-8",
                newline="\n",
            )
            if ras:
                (odir / "k").write_text(
                    field_k(patches, rbc.get("k", {}), k0, ami_patterns=ami_pats, patch_types=patch_types),
                    encoding="utf-8",
                    newline="\n",
                )
                (odir / "epsilon").write_text(
                    field_epsilon(patches, rbc.get("epsilon", {}), eps0, ami_patterns=ami_pats, patch_types=patch_types),
                    encoding="utf-8",
                    newline="\n",
                )
                (odir / "nut").write_text(
                    field_nut(patches, rbc.get("nut", {}), ami_patterns=ami_pats, patch_types=patch_types),
                    encoding="utf-8",
                    newline="\n",
                )
                (odir / "alphat").write_text(
                    field_alphat(patches, rbc.get("alphat", {}), ami_patterns=ami_pats, patch_types=patch_types),
                    encoding="utf-8",
                    newline="\n",
                )

        fv_opt = build_region_fv_options(
            region_type=reg.type,
            region_name=reg.name,
            boundary_conditions=cfg.boundary_conditions,
            numerics=cfg.numerics,
        )
        for base in (case_dir / "system" / region, case_dir / "system.orig" / region):
            base.mkdir(parents=True, exist_ok=True)
            opt_path = base / "fvOptions"
            if fv_opt:
                opt_path.write_text(fv_opt, encoding="utf-8", newline="\n")
            elif opt_path.is_file():
                opt_path.unlink()

    return {"regions": report}
