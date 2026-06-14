"""CLI entry point: python -m foam2thermal"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .case_generator import generate_case
from .config import load_config
from .interfaces import scan_cgns2foam_interfaces
from .mesh import load_mesh, validate_mesh_complete
from .runner import run_allrun_pre, run_openfoam, run_solver


def _cmd_scan(args: argparse.Namespace) -> int:
    case = Path(args.case)
    mesh = load_mesh(case)
    pairs = scan_cgns2foam_interfaces(
        mesh,
        suffix_pattern=args.suffix_pattern,
        ami_patterns=args.ami_patterns.split(",") if args.ami_patterns else None,
    )
    out = {"patches": mesh.patch_names, "cell_zones": [z.name for z in mesh.cell_zones], "interface_pairs": pairs}
    print(json.dumps(out, indent=2))
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    report = generate_case(cfg, dry_run=args.dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.dry_run:
        return 0
    print(f"\nCase written to: {cfg.output_case}")
    print("Run mesh prep:  ./Allrun.pre  (inside MSYS2 OpenFOAM environment)")
    print("Run solver:     ./Allrun")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    case_dir = cfg.output_case if not args.case else Path(args.case)

    if args.step in ("all", "prep"):
        print("Running Allrun.pre ...")
        result = run_allrun_pre(cfg.bash_exe, cfg.openfoam_root, case_dir)
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            return result.returncode

    if args.step in ("all", "solve"):
        print("Running solver ...")
        result = run_solver(cfg.bash_exe, cfg.openfoam_root, case_dir)
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            return result.returncode

    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    case = Path(args.case)
    missing = validate_mesh_complete(case)
    if missing:
        print(f"INCOMPLETE: missing {missing}")
        return 1
    mesh = load_mesh(case)
    print(f"OK: {len(mesh.patches)} patches, {len(mesh.cell_zones)} cellZones")
    for z in mesh.cell_zones:
        print(f"  cellZone {z.name}: {len(z.cell_labels)} cells")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="foam2thermal – JSON-driven chtMultiRegionSimpleFoam case setup",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Scan cgns2foam interface patch pairs")
    p_scan.add_argument("case", help="Source OpenFOAM case directory")
    p_scan.add_argument("--suffix-pattern", default=r"_\d+$")
    p_scan.add_argument("--ami-patterns", default="ami_rot\\d+")
    p_scan.set_defaults(func=_cmd_scan)

    p_build = sub.add_parser("build", help="Generate CHT case from JSON config")
    p_build.add_argument("config", help="JSON configuration file")
    p_build.add_argument("--dry-run", action="store_true", help="Only report, do not write files")
    p_build.set_defaults(func=_cmd_build)

    p_run = sub.add_parser("run", help="Execute Allrun.pre / solver via MSYS2 OpenFOAM")
    p_run.add_argument("config", help="JSON configuration file")
    p_run.add_argument("--case", help="Override output case directory")
    p_run.add_argument(
        "--step",
        choices=("all", "prep", "solve"),
        default="all",
        help="Run mesh prep only, solver only, or both",
    )
    p_run.set_defaults(func=_cmd_run)

    p_check = sub.add_parser("check", help="Validate source mesh completeness")
    p_check.add_argument("case", help="Source OpenFOAM case directory")
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
