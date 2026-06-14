"""Execute OpenFOAM utilities via MSYS2 bash."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .paths import msys_bash_cmd, win_to_msys


def _of_env(cfg_bash: Path, of_root: Path) -> str:
    of_msys = win_to_msys(of_root)
    bash_msys = win_to_msys(cfg_bash.parent)
    return (
        f"export PATH={bash_msys}:$PATH && "
        f"source {of_msys}/etc/bashrc 2>/dev/null && "
    )


def run_openfoam(
    bash_exe: Path,
    of_root: Path,
    case_dir: Path,
    command: str,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    case_msys = win_to_msys(case_dir)
    full = _of_env(bash_exe, of_root) + f"cd {case_msys} && {command}"
    argv = msys_bash_cmd(bash_exe, full)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=check,
    )


def run_allrun_pre(bash_exe: Path, of_root: Path, case_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_openfoam(bash_exe, of_root, case_dir, "chmod +x Allrun.pre Allrun Allclean && ./Allrun.pre")


def run_solver(bash_exe: Path, of_root: Path, case_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_openfoam(bash_exe, of_root, case_dir, "./Allrun")
