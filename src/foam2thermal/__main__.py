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

from .case_generator import generate_case
from .config import load_config
from .interfaces import scan_cgns2foam_interfaces
from .mesh import load_mesh, validate_mesh_complete
from .runner import openfoam_run_failed, run_allrun_pre, run_solver

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


def _cmd_scan(args: argparse.Namespace) -> int:
    input_mesh, _, output_case, cfg = _resolve_paths(args)
    mesh = load_mesh(input_mesh)
    iface_cfg = cfg.interfaces
    pairs = scan_cgns2foam_interfaces(
        mesh,
        suffix_pattern=iface_cfg.get("suffix_pattern", r"_\d+$"),
        ami_patterns=iface_cfg.get("ami_patterns", [r"ami_rot\d+"]),
        exclude=iface_cfg.get("exclude", []),
    )
    report = {
        "input_mesh": str(input_mesh.resolve()),
        "output_case": str(output_case.resolve()),
        "config": str(Path(args.config).resolve()),
        "patches": mesh.patch_names,
        "cell_zones": [z.name for z in mesh.cell_zones],
        "interface_pairs": [{"master": m, "slave": s} for m, s in pairs],
    }
    out_path = output_case / "interface_scan.json"
    _write_json(out_path, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport: {out_path}")
    return 0


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
        result = run_allrun_pre(cfg.bash_exe, cfg.openfoam_root, output_case)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if openfoam_run_failed(result):
            print("Allrun.pre failed – see log.* in output case.", file=sys.stderr)
            return 1

    if args.step in ("all", "solve"):
        print(f"Running solver in {output_case} ...")
        result = run_solver(cfg.bash_exe, cfg.openfoam_root, output_case, solver=cfg.solver)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if openfoam_run_failed(result):
            print("Solver run failed – see log.* in output case.", file=sys.stderr)
            return 1

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
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
