"""Parse OpenFOAM polyMesh metadata (boundary, cellZones)."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PatchInfo:
    name: str
    patch_type: str
    n_faces: int
    start_face: int


@dataclass
class CellZoneInfo:
    name: str
    cell_labels: list[int] = field(default_factory=list)


@dataclass
class MeshInfo:
    patches: list[PatchInfo] = field(default_factory=list)
    cell_zones: list[CellZoneInfo] = field(default_factory=list)

    @property
    def patch_names(self) -> list[str]:
        return [p.name for p in self.patches]


def _skip_foam_header(text: str) -> str:
    """Return body after the closing ``}`` of the FoamFile header."""
    idx = text.find("FoamFile")
    if idx < 0:
        return text
    brace = text.find("{", idx)
    depth = 0
    for i, ch in enumerate(text[brace:], start=brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 :]
    return text


def parse_boundary(path: Path) -> list[PatchInfo]:
    text = _skip_foam_header(path.read_text(encoding="utf-8", errors="replace"))
    patches: list[PatchInfo] = []

    # Match patch blocks: name { type ... nFaces ... startFace ... }
    for m in re.finditer(
        r"(\w[\w\.]*)\s*\{[^}]*?type\s+(\S+);[^}]*?"
        r"nFaces\s+(\d+);[^}]*?startFace\s+(\d+);",
        text,
        flags=re.DOTALL,
    ):
        patches.append(
            PatchInfo(
                name=m.group(1),
                patch_type=m.group(2),
                n_faces=int(m.group(3)),
                start_face=int(m.group(4)),
            )
        )
    return patches


def _read_binary_label_list(data: bytes, offset: int) -> tuple[list[int], int]:
    """Read OpenFOAM binary ``List<label>`` starting at *offset*."""
    pos = offset
    while pos < len(data) and data[pos : pos + 1] in (b"\n", b"\r", b" ", b"\t"):
        pos += 1
    nl = data.find(b"\n", pos)
    count = int(data[pos:nl].decode().strip())
    pos = nl + 1
    while pos < len(data) and data[pos : pos + 1] in (b"\n", b"\r", b" "):
        pos += 1
    if data[pos : pos + 1] == b"(":
        pos += 1
    labels = list(struct.unpack(f"<{count}i", data[pos : pos + count * 4]))
    pos += count * 4
    if data[pos : pos + 1] == b")":
        pos += 1
    return labels, pos


def parse_cell_zones(path: Path) -> list[CellZoneInfo]:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    body = _skip_foam_header(text)

    zones: list[CellZoneInfo] = []
    for m in re.finditer(
        r"(\w[\w\.]*)\s*\{[^}]*?cellLabels\s+List<label>",
        body,
        flags=re.DOTALL,
    ):
        name = m.group(1)
        marker = b"List<label>"
        start = raw.find(marker, m.start())
        if start < 0:
            continue
        pos = start + len(marker)
        labels, _ = _read_binary_label_list(raw, pos)
        zones.append(CellZoneInfo(name=name, cell_labels=labels))
    return zones


def load_mesh(case_dir: Path) -> MeshInfo:
    poly = case_dir / "constant" / "polyMesh"
    boundary = poly / "boundary"
    if not boundary.is_file():
        raise FileNotFoundError(f"Missing polyMesh boundary: {boundary}")

    info = MeshInfo(patches=parse_boundary(boundary))

    cell_zones_path = poly / "cellZones"
    if cell_zones_path.is_file():
        info.cell_zones = parse_cell_zones(cell_zones_path)

    return info


def validate_mesh_complete(case_dir: Path) -> list[str]:
    """Return list of missing required polyMesh files."""
    poly = case_dir / "constant" / "polyMesh"
    required = ["points", "faces", "owner", "neighbour", "boundary"]
    missing = [f for f in required if not (poly / f).is_file()]
    if not (poly / "cellZones").is_file():
        missing.append("cellZones")
    return missing
