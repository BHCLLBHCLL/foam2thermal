"""Path helpers for Windows ↔ MSYS2 OpenFOAM environment."""

from __future__ import annotations

import re
from pathlib import Path


def win_to_msys(path: Path | str) -> str:
    """Convert a Windows path to MSYS2 ``/c/...`` form."""
    p = Path(path).resolve()
    s = str(p).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", s):
        drive = s[0].lower()
        return f"/{drive}{s[2:]}"
    return s


def msys_bash_cmd(bash_exe: Path, command: str) -> list[str]:
    """Build argv for ``bash.exe -lc '<command>'``."""
    return [str(bash_exe), "-lc", command]
