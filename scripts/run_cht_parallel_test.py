#!/usr/bin/env python3
"""Build, prep, and run parallel chtMultiRegionSimpleFoam until endTime is reached."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from foam2thermal.case_generator import generate_case  # noqa: E402
from foam2thermal.config import load_config  # noqa: E402
from foam2thermal.runner import (  # noqa: E402
    _clean_solver_artifacts,
    openfoam_run_failed,
    reconstruct_complete,
    run_allrun_pre,
    run_solver,
    solver_reached_time,
    tail_solver_log,
    validate_temperature_results,
)


def _clean_parallel(case_dir: Path) -> None:
    _clean_solver_artifacts(case_dir)


def _patch_case_numerics(cfg, case_dir: Path) -> None:
    from foam2thermal.templates import (
        build_region_fv_options,
        control_dict,
        fv_solution_fluid,
        fv_solution_solid,
    )

    p0 = cfg.initial.get("p", 101325)
    (case_dir / "system" / "controlDict").write_text(
        control_dict(cfg.numerics, cfg.solver), encoding="utf-8"
    )
    for reg in cfg.regions:
        for base in (case_dir / "system" / reg.foam_name, case_dir / "system.orig" / reg.foam_name):
            base.mkdir(parents=True, exist_ok=True)
            if reg.type == "fluid":
                (base / "fvSolution").write_text(
                    fv_solution_fluid(cfg.numerics, p_ref=p0), encoding="utf-8"
                )
            else:
                (base / "fvSolution").write_text(fv_solution_solid(), encoding="utf-8")
            fv_opt = build_region_fv_options(
                region_type=reg.type,
                region_name=reg.name,
                boundary_conditions=cfg.boundary_conditions,
                numerics=cfg.numerics,
            )
            opt_path = base / "fvOptions"
            if fv_opt:
                opt_path.write_text(fv_opt, encoding="utf-8")
            elif opt_path.is_file():
                opt_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_mesh",
        nargs="?",
        default="tests/laptop_thermal_steady_scaled_v3_orig",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="configs/laptop_thermal_steady_v3.json",
    )
    parser.add_argument(
        "output_case",
        nargs="?",
        default="cases/laptop_thermal_cht_v3",
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-prep", action="store_true")
    args = parser.parse_args()

    input_mesh = (ROOT / args.input_mesh).resolve()
    config_path = (ROOT / args.config).resolve()
    output_case = (ROOT / args.output_case).resolve()
    cfg = load_config(config_path, input_mesh, output_case)
    end_time = int(cfg.numerics.get("endTime", 200))
    t0 = float(cfg.initial.get("T", 300))

    print(f"Input mesh : {input_mesh}")
    print(f"Config     : {config_path}")
    print(f"Output case: {output_case}")
    print(f"Target     : Time={end_time}, nProcs={cfg.n_procs}")
    print(f"Python     : {cfg.python_exe}")

    if not args.skip_build:
        print("\n=== build ===")
        generate_case(cfg)
    elif not output_case.is_dir():
        print(f"Output case missing: {output_case}", file=sys.stderr)
        return 1

    if not args.skip_prep:
        print("\n=== prep (Allrun.pre) ===")
        _clean_parallel(output_case)
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
            print("Allrun.pre failed", file=sys.stderr)
            return 1

    print("\n=== solve (decomposePar + runParallel + reconstruct) ===")
    _patch_case_numerics(cfg, output_case)
    result = run_solver(
        cfg.bash_exe,
        cfg.openfoam_root,
        output_case,
        solver=cfg.solver,
        parallel=True,
        n_procs=cfg.n_procs,
        reconstruct=True,
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    log_tail = tail_solver_log(output_case, solver=cfg.solver)
    if log_tail:
        print("\n--- log tail ---")
        print(log_tail)

    if openfoam_run_failed(result):
        print("Solver failed", file=sys.stderr)
        return 1
    if not solver_reached_time(output_case, end_time, solver=cfg.solver):
        print(f"Solver did not reach Time={end_time}", file=sys.stderr)
        return 1

    region_names = [r.foam_name for r in cfg.regions]
    if not reconstruct_complete(output_case, region_names):
        print("Reconstruction incomplete – check log.reconstructPar*", file=sys.stderr)
        return 1

    print(f"\nPASS: {cfg.solver} reached Time={end_time} with {cfg.n_procs} processes.")
    print("PASS: parallel regions reconstructed to case root.")

    print("\n=== temperature validation ===")
    temp_ok, temp_msgs = validate_temperature_results(output_case, end_time=end_time, t0=t0)
    for line in temp_msgs:
        print(f"  {line}")
    if not temp_ok:
        print("FAIL: temperature validation", file=sys.stderr)
        return 1
    print("PASS: temperature validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
