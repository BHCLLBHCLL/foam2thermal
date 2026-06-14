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

    # cgns2foam / ANSA: startFace may appear before or after nFaces
    block_re = re.compile(
        r"(\w[\w\.]*)\s*\{[^}]*?type\s+(\S+);[^}]*?"
        r"(?:nFaces\s+(\d+);[^}]*?startFace\s+(\d+)|"
        r"startFace\s+(\d+);[^}]*?nFaces\s+(\d+));",
        flags=re.DOTALL,
    )
    for m in block_re.finditer(text):
        if m.group(3) is not None:
            n_faces, start_face = int(m.group(3)), int(m.group(4))
        else:
            start_face, n_faces = int(m.group(5)), int(m.group(6))
        patches.append(
            PatchInfo(
                name=m.group(1),
                patch_type=m.group(2),
                n_faces=n_faces,
                start_face=start_face,
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
        r"([^\s(\{\t\n]+)\s*\{\s*type\s+cellZone;\s*cellLabels\s+List<label>",
        body,
        flags=re.DOTALL,
    ):
        name = m.group(1).strip()
        marker = b"List<label>"
        start = raw.find(marker, m.start())
        if start < 0:
            continue
        pos = start + len(marker)
        labels, _ = _read_binary_label_list(raw, pos)
        zones.append(CellZoneInfo(name=name, cell_labels=labels))
    return zones


def write_cell_zones_v2412(path: Path, zones: list[CellZoneInfo]) -> None:
    """Rewrite cellZones in OpenFOAM v2412-readable form.

    cgns2foam may emit ``List<label>377146`` without a newline before the
    count; OpenFOAM v2412 rejects that with 'ill defined primitiveEntry'.
    """
    import numpy as np

    header = (
        "/*--------------------------------*- C++ -*----------------------------------*\\\n"
        "| =========                 |                                                 |\n"
        "| \\\\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox           |\n"
        "|  \\\\    /   O peration     | Version:  v2412                                 |\n"
        "|   \\\\  /    A nd           | Website:  www.openfoam.com                      |\n"
        "|    \\/     M anipulation  |                                                 |\n"
        "\\*---------------------------------------------------------------------------*/\n"
        "FoamFile\n"
        "{\n"
        "    version     2.0;\n"
        "    format      binary;\n"
        "    arch        \"LSB;label=32;scalar=64\";\n"
        "    class       regIOobject;\n"
        "    location    \"constant/polyMesh\";\n"
        "    object      cellZones;\n"
        "}\n"
        "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n\n"
    )
    with open(path, "wb") as fh:
        fh.write(header.encode("ascii"))
        fh.write(f"{len(zones)}\n(\n".encode("ascii"))
        for z in zones:
            arr = np.ascontiguousarray(z.cell_labels, dtype=np.int32)
            fh.write(f"\t{z.name}\n\t{{\n".encode("ascii"))
            fh.write(b"\t\ttype cellZone;\n")
            fh.write(b"\t\tcellLabels\tList<label>\n")
            fh.write(f"{arr.size}\n(".encode("ascii"))
            fh.write(arr.tobytes(order="C"))
            fh.write(b")\n\t;\n\t}\n")
        fh.write(b")\n")


def repair_cell_zones(poly_dir: Path) -> None:
    """Fix cgns2foam cellZones header (List<label>N -> List<label>\\nN)."""
    cz = poly_dir / "cellZones"
    if not cz.is_file():
        return
    raw = cz.read_bytes()
    fixed = re.sub(rb"List<label>(\d+)\s*\n", rb"List<label>\n\1\n", raw, count=0)
    if fixed != raw:
        cz.write_bytes(fixed)
        return
    zones = parse_cell_zones(cz)
    if zones and b"type cellZone" not in raw:
        write_cell_zones_v2412(cz, zones)


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
