"""CLI entry point: python -m foam2thermal

Usage::

    setup_cht_case.py <command> <input_mesh> <config.json> <output_case> [options]

Examples::

    setup_cht_case.py check tests/laptop_thermal_steady_orig_fix configs/laptop.json cases/out
    setup_cht_case.py scan  tests/laptop_thermal_steady_orig_fix configs/laptop.json cases/out
    setup_cht_case.py build tests/laptop_thermal_steady_orig_fix configs/laptop.json cases/out
    setup_cht_case.py run    tests/laptop_thermal_steady_orig_fix configs/laptop.json cases/out --step prep
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .case_generator import generate_case, cell_zone_to_config_region
from .config import CaseConfig, load_config
from .interfaces import scan_interfaces_report
from .mesh import (
    build_patch_region_map,
    infer_patch_regions_from_topology,
    load_mesh,
    load_region_properties,
    validate_mesh_complete,
)
from .runner import (
    openfoam_run_failed,
    reconstruct_complete,
    run_allrun_pre,
    run_reconstruct_parallel,
    run_solver,
    solver_reached_time,
    tail_solver_log,
)

EPILOG = """
positional arguments (all subcommands):
  input_mesh    OpenFOAM single-block mesh directory from cgns2foam (constant/polyMesh)
  config        JSON: regions, materials, interfaces, numerics, openfoam paths
  output_case   Output CHT case directory (build/run write here; scan/check write reports)

examples:
  %(prog)s check tests/laptop_thermal_steady_orig_fix configs/laptop_thermal_steady.json cases/laptop_cht
  %(prog)s scan  tests/laptop_thermal_steady_orig_fix configs/laptop_thermal_steady.json cases/laptop_cht
  %(prog)s build tests/laptop_thermal_steady_orig_fix configs/laptop_thermal_steady.json cases/laptop_cht
  %(prog)s run   tests/laptop_thermal_steady_orig_fix configs/laptop_thermal_steady.json cases/laptop_cht
""".strip()


def _resolve_paths(args: argparse.Namespace):
    input_mesh = Path(args.input_mesh)
    config_path = Path(args.config)
    output_case = Path(args.output_case)
    cfg = load_config(config_path, input_mesh, output_case)
    return input_mesh, config_path, output_case, cfg


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _cmd_check(args: argparse.Namespace) -> int:
    input_mesh, _, output_case, _ = _resolve_paths(args)
    missing = validate_mesh_complete(input_mesh)
    report = {
        "input_mesh": str(input_mesh.resolve()),
        "output_case": str(output_case.resolve()),
        "status": "ok" if not missing else "incomplete",
        "missing_files": missing,
    }
    if missing:
        _write_json(output_case / "mesh_check.json", report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\nReport: {output_case / 'mesh_check.json'}", file=sys.stderr)
        return 1

    mesh = load_mesh(input_mesh)
    report["patches"] = len(mesh.patches)
    report["cell_zones"] = [
        {"name": z.name, "n_cells": len(z.cell_labels)} for z in mesh.cell_zones
    ]
    _write_json(output_case / "mesh_check.json", report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport: {output_case / 'mesh_check.json'}")
    return 0


def _resolve_region_type(cfg: CaseConfig, rp_types: dict[str, str]):
    def resolve(name: str | None) -> str:
        if not name:
            return "unknown"
        if name in rp_types:
            return rp_types[name]
        t = cfg.resolve_region_type(name)
        if t != "unknown":
            return t
        return "unknown"

    return resolve


def _cmd_scan(args: argparse.Namespace) -> int:
    input_mesh, _, output_case, cfg = _resolve_paths(args)
    mesh = load_mesh(input_mesh)
    rp = load_region_properties(input_mesh)
    rp_types = rp.region_types if rp else {}
    topo = infer_patch_regions_from_topology(input_mesh, mesh)
    patch_region = dict(topo)
    for patch, region in cfg.patch_regions.items():
        patch_region.setdefault(patch, region)
    if rp is None:
        zone_map = cell_zone_to_config_region(cfg)
        patch_region = build_patch_region_map(
            input_mesh,
            mesh,
            explicit=cfg.patch_regions,
            cell_zone_to_region=zone_map or None,
            name_heuristic=lambda p: _name_heuristic_patch_region(p, cfg),
        )
    else:
        for p in mesh.patch_names:
            if p not in patch_region:
                guessed = _name_heuristic_patch_region(p, cfg)
                if guessed and guessed in rp_types:
                    patch_region[p] = guessed
                elif guessed:
                    patch_region[p] = guessed
    resolve_type = _resolve_region_type(cfg, rp_types)
    scan_body = scan_interfaces_report(
        mesh,
        input_mesh,
        cfg.raw,
        resolve_region_type=resolve_type,
        patch_region=patch_region,
        region_properties=(
            {"fluid": rp.fluid, "solid": rp.solid} if rp else None
        ),
    )
    report = {
        "input_mesh": str(input_mesh.resolve()),
        "output_case": str(output_case.resolve()),
        "config": str(Path(args.config).resolve()),
        **scan_body,
    }
    out_path = output_case / "interface_scan.json"
    _write_json(out_path, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport: {out_path}")
    return 0


def _name_heuristic_patch_region(patch: str, cfg: CaseConfig) -> str | None:
    """Fallback patch→region when topology is inconclusive (mirrors build)."""
    import re

    if patch in cfg.patch_regions:
        return cfg.patch_regions[patch]
    base = re.sub(r"_\d+$", "", patch)
    if "ami" in base.lower() or base.startswith("open") or base.startswith("impeller"):
        for r in cfg.fluid_regions:
            if r == "air":
                return r
        return cfg.fluid_regions[0] if cfg.fluid_regions else None
    # Match configured region names (fluid or solid) by patch name prefix.
    for r in list(cfg.fluid_regions) + list(cfg.solid_regions):
        if base.lower().startswith(r.lower()):
            return r
    return None


def _cmd_build(args: argparse.Namespace) -> int:
    _, _, output_case, cfg = _resolve_paths(args)
    report = generate_case(cfg, dry_run=args.dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.dry_run:
        return 0
    print(f"\nInput mesh : {cfg.source_case}")
    print(f"Output case: {output_case}")
    print("Next (MSYS2 OpenFOAM):")
    print(f"  cd {output_case}")
    print("  ./Allrun.pre   # stitch + split regions")
    print("  ./Allrun       # chtMultiRegionSimpleFoam")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    _, _, output_case, cfg = _resolve_paths(args)
    if not output_case.is_dir():
        print(f"Output case not found: {output_case}", file=sys.stderr)
        print("Run build first.", file=sys.stderr)
        return 1

    if args.step in ("all", "prep"):
        print(f"Running Allrun.pre in {output_case} ...")
        result = run_allrun_pre(
            cfg.bash_exe,
            cfg.openfoam_root,
            output_case,
            python_exe=cfg.python_exe,
        )
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if openfoam_run_failed(result):
            print("Allrun.pre failed – see log.* in output case.", file=sys.stderr)
            return 1

    if args.step in ("all", "solve"):
        parallel = (args.parallel or cfg.n_procs > 1) and not args.serial
        mode = f"parallel ({cfg.n_procs} procs)" if parallel else "serial"
        print(f"Running solver in {output_case} [{mode}] ...")
        result = run_solver(
            cfg.bash_exe,
            cfg.openfoam_root,
            output_case,
            solver=cfg.solver,
            parallel=parallel,
            n_procs=cfg.n_procs,
            reconstruct=parallel and not args.no_reconstruct,
        )
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if openfoam_run_failed(result):
            print("Solver run failed – see log.* in output case.", file=sys.stderr)
            tail = tail_solver_log(output_case, solver=cfg.solver)
            if tail:
                print("\n--- log tail ---\n" + tail, file=sys.stderr)
            return 1
        target = int(cfg.numerics.get("endTime", 200))
        if not solver_reached_time(output_case, target, solver=cfg.solver):
            print(
                f"Solver finished but log does not show Time={target} – check log.{cfg.solver}",
                file=sys.stderr,
            )
            tail = tail_solver_log(output_case, solver=cfg.solver)
            if tail:
                print("\n--- log tail ---\n" + tail, file=sys.stderr)
            return 1
        print(f"Solver reached Time={target} successfully.")
        if parallel and not args.no_reconstruct:
            region_names = [r.foam_name for r in cfg.regions]
            if reconstruct_complete(output_case, region_names):
                print("Parallel reconstruction complete (fields merged to case root).")
            else:
                print("Warning: reconstructPar may have failed – check log.reconstructPar*", file=sys.stderr)

    return 0


def _add_io_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "input_mesh",
        help="input OpenFOAM mesh case directory (cgns2foam output)",
    )
    parser.add_argument(
        "config",
        help="JSON configuration file (regions, materials, interfaces, numerics)",
    )
    parser.add_argument(
        "output_case",
        help="output CHT case directory",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup_cht_case.py",
        description="foam2thermal – build chtMultiRegionSimpleFoam cases from mesh + JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="validate input polyMesh completeness")
    _add_io_args(p_check)
    p_check.set_defaults(func=_cmd_check)

    p_scan = sub.add_parser("scan", help="scan cgns2foam interface patch pairs")
    _add_io_args(p_scan)
    p_scan.set_defaults(func=_cmd_scan)

    p_build = sub.add_parser("build", help="generate CHT case into output directory")
    _add_io_args(p_build)
    p_build.add_argument(
        "--dry-run",
        action="store_true",
        help="analyse only, do not write output case",
    )
    p_build.set_defaults(func=_cmd_build)

    p_run = sub.add_parser("run", help="run Allrun.pre and/or solver via MSYS2 OpenFOAM")
    _add_io_args(p_run)
    p_run.add_argument(
        "--step",
        choices=("all", "prep", "solve"),
        default="all",
        help="prep=Allrun.pre only, solve=solver only, all=both (default)",
    )
    p_run.add_argument(
        "--parallel",
        action="store_true",
        help="run decomposePar + runParallel (default when openfoam.nProcs > 1)",
    )
    p_run.add_argument(
        "--serial",
        action="store_true",
        help="force serial runApplication even if nProcs > 1",
    )
    p_run.add_argument(
        "--no-reconstruct",
        action="store_true",
        help="skip reconstructParMesh/reconstructPar after parallel solve",
    )
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
