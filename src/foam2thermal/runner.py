"""Execute OpenFOAM utilities via MSYS2 bash."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .paths import msys_bash_cmd, win_to_msys

_FATAL_RE = re.compile(r"FOAM (FATAL|aborting)", re.IGNORECASE)


def _of_env(cfg_bash: Path, of_root: Path) -> str:
    of_msys = win_to_msys(of_root)
    bash_msys = win_to_msys(cfg_bash.parent)
    return (
        f"export FOAM_SIGFPE=0 FOAM_SETNAN=0 && "
        f"export PATH={bash_msys}:$PATH && "
        f"source {of_msys}/etc/bashrc 2>/dev/null && "
    )


def openfoam_run_failed(result: subprocess.CompletedProcess[str]) -> bool:
    text = (result.stdout or "") + (result.stderr or "")
    return result.returncode != 0 or bool(_FATAL_RE.search(text))


def run_openfoam(
    bash_exe: Path,
    of_root: Path,
    case_dir: Path,
    command: str,
) -> subprocess.CompletedProcess[str]:
    case_msys = win_to_msys(case_dir)
    full = _of_env(bash_exe, of_root) + f"cd {case_msys} && {command}"
    argv = msys_bash_cmd(bash_exe, full)
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def run_verify_regions(bash_exe: Path, of_root: Path, case_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_openfoam(
        bash_exe,
        of_root,
        case_dir,
        "sh scripts/verifyRegions.sh",
    )


def run_allrun_pre(bash_exe: Path, of_root: Path, case_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_openfoam(
        bash_exe,
        of_root,
        case_dir,
        "chmod +x Allrun.pre Allrun Allclean && ./Allrun.pre",
    )


def _clean_solver_artifacts(case_dir: Path, *, solver: str = "chtMultiRegionSimpleFoam") -> None:
    for name in (
        f"log.{solver}",
        "log.decomposePar.decomposePar",
        f"log.decomposePar",
    ):
        path = case_dir / name
        if path.is_file():
            path.unlink()
    for path in case_dir.glob("processor*"):
        if path.is_dir():
            shutil.rmtree(path)
    for path in case_dir.glob("[0-9]*"):
        if path.is_dir() and path.name != "0.orig":
            shutil.rmtree(path)


def run_restore_zero(
    bash_exe: Path,
    of_root: Path,
    case_dir: Path,
) -> subprocess.CompletedProcess[str]:
    return run_openfoam(
        bash_exe,
        of_root,
        case_dir,
        ". ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions && restore0Dir -allRegions",
    )


def run_reconstruct_parallel(
    bash_exe: Path,
    of_root: Path,
    case_dir: Path,
) -> subprocess.CompletedProcess[str]:
    """Merge decomposed mesh/fields from processor* back to the case root."""
    return run_openfoam(
        bash_exe,
        of_root,
        case_dir,
        (
            ". ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions && "
            "runApplication -o -s reconstructParMesh "
            "reconstructParMesh -allRegions -constant && "
            "runApplication -o -s reconstructPar reconstructPar -allRegions"
        ),
    )


def reconstruct_complete(case_dir: Path, regions: list[str]) -> bool:
    """Return True when reconstructed time/constant dirs exist at case root."""
    if not any(case_dir.glob("processor*")):
        return False
    for region in regions:
        poly = case_dir / "constant" / region / "polyMesh" / "points"
        if not poly.is_file():
            return False
    time_dirs = [p for p in case_dir.iterdir() if p.is_dir() and p.name.isdigit()]
    if not time_dirs:
        return (case_dir / "0" / regions[0]).is_dir()
    latest = max(time_dirs, key=lambda p: int(p.name))
    return (latest / regions[0]).is_dir()


def run_solver(
    bash_exe: Path,
    of_root: Path,
    case_dir: Path,
    *,
    solver: str = "chtMultiRegionSimpleFoam",
    parallel: bool = False,
    n_procs: int = 8,
    clean: bool = True,
    reconstruct: bool = True,
) -> subprocess.CompletedProcess[str]:
    if clean:
        _clean_solver_artifacts(case_dir, solver=solver)
        restore = run_restore_zero(bash_exe, of_root, case_dir)
        if openfoam_run_failed(restore):
            return restore
    if parallel:
        result = run_openfoam(
            bash_exe,
            of_root,
            case_dir,
            (
                f". ${{WM_PROJECT_DIR:?}}/bin/tools/RunFunctions && "
                f"runApplication -o -s decomposePar decomposePar -allRegions -copyZero -force && "
                f"runParallel -o -np {n_procs} {solver}"
            ),
        )
        if openfoam_run_failed(result) or not reconstruct:
            return result
        recon = run_reconstruct_parallel(bash_exe, of_root, case_dir)
        if openfoam_run_failed(recon):
            return recon
        stdout = (result.stdout or "") + (recon.stdout or "")
        stderr = (result.stderr or "") + (recon.stderr or "")
        return subprocess.CompletedProcess(
            args=result.args,
            returncode=recon.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    return run_openfoam(
        bash_exe,
        of_root,
        case_dir,
        f". ${{WM_PROJECT_DIR:?}}/bin/tools/RunFunctions && runApplication -o {solver}",
    )


def solver_log_path(case_dir: Path, solver: str = "chtMultiRegionSimpleFoam") -> Path:
    return case_dir / f"log.{solver}"


def solver_reached_time(case_dir: Path, target_time: int, *, solver: str = "chtMultiRegionSimpleFoam") -> bool:
    log_path = solver_log_path(case_dir, solver)
    if not log_path.is_file():
        return False
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if re.search(rf"\bTime\s*=\s*{target_time}\b", text) is None:
        return False
    return bool(re.search(r"End\b", text))


def tail_solver_log(case_dir: Path, *, solver: str = "chtMultiRegionSimpleFoam", lines: int = 40) -> str:
    log_path = solver_log_path(case_dir, solver)
    if not log_path.is_file():
        return ""
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])
