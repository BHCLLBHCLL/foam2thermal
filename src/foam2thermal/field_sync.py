"""Sync 0.orig fields with regional polyMesh boundary patches after split."""

from __future__ import annotations

import json
from pathlib import Path

from .config import load_config
from .mesh import parse_boundary
from .templates import (
    field_alphat,
    field_epsilon,
    field_k,
    field_nut,
    field_p,
    field_p_rgh,
    field_T,
    field_U,
)


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
    ami_pats = cfg.interfaces.get("ami_patterns", [r"ami_rot\d+"])
    ras = str(cfg.turbulence.get("simulationType", "laminar")).lower() not in ("laminar", "")

    by_foam = {r.foam_name: r for r in cfg.regions}
    report: dict[str, list[str]] = {}

    for bnd_path in sorted(case_dir.glob("constant/*/polyMesh/boundary")):
        region = bnd_path.parent.parent.name
        reg = by_foam.get(region)
        if not reg:
            continue
        patches = [p.name for p in parse_boundary(bnd_path)]
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
                field_p_rgh(patches, 0, bc_cfg=rbc.get("p_rgh", {}), ami_patterns=ami_pats),
                encoding="utf-8",
                newline="\n",
            )
            if ras:
                (odir / "k").write_text(
                    field_k(patches, rbc.get("k", {}), k0, ami_patterns=ami_pats),
                    encoding="utf-8",
                    newline="\n",
                )
                (odir / "epsilon").write_text(
                    field_epsilon(patches, rbc.get("epsilon", {}), eps0, ami_patterns=ami_pats),
                    encoding="utf-8",
                    newline="\n",
                )
                (odir / "nut").write_text(
                    field_nut(patches, rbc.get("nut", {}), ami_patterns=ami_pats),
                    encoding="utf-8",
                    newline="\n",
                )
                (odir / "alphat").write_text(
                    field_alphat(patches, rbc.get("alphat", {}), ami_patterns=ami_pats),
                    encoding="utf-8",
                    newline="\n",
                )

    return {"regions": report}
