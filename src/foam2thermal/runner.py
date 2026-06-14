"""Execute OpenFOAM utilities via MSYS2 bash."""

from __future__ import annotations

import re
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


def run_solver(bash_exe: Path, of_root: Path, case_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_openfoam(bash_exe, of_root, case_dir, "./Allrun")
