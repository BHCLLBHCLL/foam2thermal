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
    sample_mode: str | None = None
    sample_region: str | None = None
    sample_patch: str | None = None
    neighbour_patch: str | None = None
    rotation_axis: tuple[float, float, float] | None = None
    match_tolerance: float | None = None
    transform: str | None = None


def coupling_patch_name(local_region: str, remote_region: str) -> str:
    return f"{local_region}_to_{remote_region}"


def parse_coupling_patch(name: str, region_names: list[str]) -> tuple[str, str] | None:
    """Return (local_region, remote_region) for a ``*_to_*`` patch name."""
    for local in region_names:
        prefix = f"{local}_to_"
        if name.startswith(prefix):
            return local, name[len(prefix) :]
    return None


def mapped_wall_patch(
    local_region: str,
    remote_region: str,
    *,
    n_faces: int,
    start_face: int,
) -> PatchInfo:
    return PatchInfo(
        name=coupling_patch_name(local_region, remote_region),
        patch_type="mappedWall",
        n_faces=n_faces,
        start_face=start_face,
        sample_mode="nearestPatchFace",
        sample_region=remote_region,
        sample_patch=coupling_patch_name(remote_region, local_region),
    )


def cyclic_ami_patch(
    name: str,
    neighbour: str,
    *,
    n_faces: int,
    start_face: int,
    rotation_axis: tuple[float, float, float] = (0.0, 0.0, 1.0),
    match_tolerance: float = 0.001,
    transform: str = "noOrdering",
) -> PatchInfo:
    return PatchInfo(
        name=name,
        patch_type="cyclicAMI",
        n_faces=n_faces,
        start_face=start_face,
        neighbour_patch=neighbour,
        rotation_axis=rotation_axis,
        match_tolerance=match_tolerance,
        transform=transform,
    )


def boundary_header_text(path: Path) -> str:
    """FoamFile header only (no patch count/list) for rewriting boundary."""
    text = path.read_text(encoding="utf-8", errors="replace")
    end = text.find("\n(\n")
    if end >= 0:
        chunk = text[:end]
        chunk = re.sub(r"(\n\d+\s*)+$", "", chunk)
        return chunk + "\n"
    return text.split("(")[0].rstrip() + "\n"


def write_boundary(path: Path, patches: list[PatchInfo], header_text: str) -> None:
    lines = [header_text, f"{len(patches)}\n(\n"]
    for p in patches:
        lines.append(f"\n\t{p.name}\n\t{{\n")
        lines.append(f"\t\ttype            {p.patch_type};\n")
        if p.patch_type == "mappedWall":
            lines.append(f"\t\tsampleMode      {p.sample_mode or 'nearestPatchFace'};\n")
            lines.append(f"\t\tsampleRegion    {p.sample_region};\n")
            lines.append(f"\t\tsamplePatch     {p.sample_patch};\n")
        elif p.patch_type == "cyclicAMI":
            ax = p.rotation_axis or (0.0, 0.0, 1.0)
            lines.append(f"\t\tneighbourPatch  {p.neighbour_patch};\n")
            lines.append(f"\t\tmatchTolerance  {p.match_tolerance or 0.001};\n")
            lines.append(f"\t\ttransform       {p.transform or 'noOrdering'};\n")
            lines.append(f"\t\trotationAxis    ({ax[0]} {ax[1]} {ax[2]});\n")
        lines.append(f"\t\tnFaces          {p.n_faces};\n")
        lines.append(f"\t\tstartFace       {p.start_face};\n")
        lines.append("\t}\n")
    lines.append(")\n")
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


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
        block_start = m.start()
        block_end = m.end()
        block = text[block_start:block_end]
        neighbour = None
        nm = re.search(r"neighbourPatch\s+(\S+);", block)
        if nm:
            neighbour = nm.group(1)
        axis = None
        am = re.search(
            r"rotationAxis\s+\(([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\);", block
        )
        if am:
            axis = (float(am.group(1)), float(am.group(2)), float(am.group(3)))
        mt = re.search(r"matchTolerance\s+([\d.eE+-]+);", block)
        match_tol = float(mt.group(1)) if mt else None
        tr = re.search(r"transform\s+(\S+);", block)
        transform = tr.group(1) if tr else None
        sample_mode = None
        sample_region = None
        sample_patch = None
        sm = re.search(r"sampleMode\s+(\S+);", block)
        if sm:
            sample_mode = sm.group(1)
        sr = re.search(r"sampleRegion\s+(\S+);", block)
        if sr:
            sample_region = sr.group(1)
        sp = re.search(r"samplePatch\s+(\S+);", block)
        if sp:
            sample_patch = sp.group(1)
        patches.append(
            PatchInfo(
                name=m.group(1),
                patch_type=m.group(2),
                n_faces=n_faces,
                start_face=start_face,
                sample_mode=sample_mode,
                sample_region=sample_region,
                sample_patch=sample_patch,
                neighbour_patch=neighbour,
                rotation_axis=axis,
                match_tolerance=match_tol,
                transform=transform,
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

    zones: list[CellZoneInfo] = []
    # Use bytes.find to locate cellZone blocks. The previous regex with
    # re.DOTALL had catastrophic backtracking on binary data (~200s for a
    # 27MB file vs <0.01s with bytes.find).
    needle = b"type cellZone;"
    search_from = 0
    while True:
        idx = raw.find(needle, search_from)
        if idx < 0:
            break
        # Find "List<label>" after "type cellZone;" (should be within ~80 bytes).
        list_pos = raw.find(b"List<label>", idx)
        if list_pos < 0 or list_pos > idx + 200:
            search_from = idx + len(needle)
            continue
        # Parse the binary label list starting right after "List<label>".
        labels, end_pos = _read_binary_label_list(raw, list_pos + len(b"List<label>"))
        # Parse backwards to find the zone name: the token before the "{"
        # that precedes "type cellZone;".  Format:
        #   \t<name>\n\t{\n\t\ttype cellZone;
        brace_pos = raw.rfind(b"{", search_from, idx)
        if brace_pos >= 0:
            # Skip whitespace (newline + tab) between name and "{".
            name_end = brace_pos
            while name_end > 0 and raw[name_end - 1 : name_end] in (
                b" ",
                b"\t",
                b"\n",
                b"\r",
            ):
                name_end -= 1
            # Scan backwards for the name token.
            name_start = name_end
            while name_start > 0 and raw[name_start - 1 : name_start] not in (
                b" ",
                b"\t",
                b"\n",
                b"\r",
            ):
                name_start -= 1
            name = raw[name_start:name_end].decode("ascii", errors="replace").strip()
        else:
            name = f"zone_{len(zones)}"
        zones.append(CellZoneInfo(name=name, cell_labels=labels))
        search_from = end_pos
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


def zone_bbox_centroid(poly_dir: Path, zone_names: list[str]) -> tuple[float, float, float]:
    """Bounding-box centre of one or more cellZones (for MRF origin).

    Vectorised: uses numpy ``np.isin`` to find zone-owned faces instead of a
    Python loop over all faces (which is O(nFaces) ~ 22M and prohibitively slow).
    """
    import numpy as np

    from .mesh_coalesce import (
        _read_binary_label_list,
        _read_binary_vector_field,
        _read_faces,
    )

    points = _read_binary_vector_field(poly_dir / "points")
    zones = {z.name: z for z in parse_cell_zones(poly_dir / "cellZones")}
    cell_labels = np.concatenate(
        [
            np.asarray(zones[n].cell_labels, dtype=np.int64)
            for n in zone_names
            if n in zones and zones[n].cell_labels
        ]
    ) if any(n in zones and zones[n].cell_labels for n in zone_names) else np.empty(0, dtype=np.int64)
    if cell_labels.size == 0:
        return (0.0, 0.0, 0.0)

    owner = _read_binary_label_list(poly_dir / "owner")
    offsets, conn = _read_faces(poly_dir / "faces")

    # Vectorised face selection: boolean mask of faces owned by zone cells.
    mask = np.isin(owner, cell_labels)
    face_idx = np.where(mask)[0]
    if face_idx.size == 0:
        return (0.0, 0.0, 0.0)

    starts = offsets[face_idx]
    ends = offsets[face_idx + 1]
    sizes = ends - starts

    # Gather vertex indices from the compact connectivity array.
    if sizes.size > 0 and np.all(sizes == sizes[0]):
        # All matching faces share the same vertex count (e.g. all quads).
        sz = int(sizes[0])
        grid = starts[:, None].astype(np.int64) + np.arange(sz, dtype=np.int64)[None, :]
        used_pts = np.unique(conn[grid.ravel()])
    else:
        # Variable face sizes: build a flat index array. The loop only runs
        # over matching faces (far fewer than nFaces), not all faces.
        total = int(sizes.sum())
        idx_arr = np.empty(total, dtype=np.int64)
        pos = 0
        for s, e in zip(starts.tolist(), ends.tolist()):
            idx_arr[pos:pos + (e - s)] = conn[s:e]
            pos += (e - s)
        used_pts = np.unique(idx_arr)

    pts = points[used_pts]
    c = 0.5 * (pts.min(axis=0) + pts.max(axis=0))
    return (float(c[0]), float(c[1]), float(c[2]))


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
