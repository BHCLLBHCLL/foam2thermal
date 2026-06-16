"""Split a monolithic polyMesh into per-region meshes (Windows-safe fallback)."""

from __future__ import annotations

import re
import shutil
from collections import defaultdict
from pathlib import Path

import numpy as np

from .mesh import (
    CellZoneInfo,
    PatchInfo,
    mapped_wall_patch,
    parse_boundary,
    parse_cell_zones,
    write_boundary,
    write_cell_zones_v2412,
)
from .mesh_coalesce import (
    _read_faces,
    _read_binary_label_list,
    _read_binary_vector_field,
    _write_binary_compact_face_list,
    _write_binary_label_list,
    _write_binary_vector_field,
)

_LABEL_IO_COUNT = re.compile(rb"\n(\d+)\s*\n\(")


def _default_header(obj_class: str, obj_name: str, *, fmt: str = "binary") -> bytes:
    return (
        f"FoamFile\n{{\n    version 2.0;\n    format {fmt};\n"
        f"    class {obj_class};\n    object {obj_name};\n}}\n\n"
    ).encode("ascii")


def _default_boundary_header() -> str:
    return (
        "FoamFile\n{\n    version 2.0;\n    format binary;\n"
        "    class polyBoundaryMesh;\n    object boundary;\n}\n\n"
    )


def _read_label_io_list(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    m = _LABEL_IO_COUNT.search(raw)
    if not m:
        raise ValueError(f"Cannot parse label list in {path}")
    n = int(m.group(1))
    return np.frombuffer(raw, dtype="<i4", count=n, offset=m.end()).copy()


def _region_names_from_properties(case_dir: Path) -> list[str]:
    for rel in ("constant/regionProperties", "system/regionProperties"):
        rp = case_dir / rel
        if not rp.is_file():
            continue
        text = rp.read_text(encoding="utf-8", errors="replace")
        names: list[str] = []
        for block in ("fluid", "solid"):
            m = re.search(rf"{block}\s*\(\s*([^)]+)\)", text, flags=re.DOTALL)
            if not m:
                continue
            chunk = m.group(1).strip()
            quoted = re.findall(r'"([^"]+)"', chunk)
            names.extend(quoted if quoted else chunk.split())
        if names:
            return names
    raise FileNotFoundError("regionProperties not found")


def _cell_region_map(case_dir: Path, poly_dir: Path, n_cells: int) -> tuple[np.ndarray, list[str]]:
    ctr = case_dir / "constant" / "cellToRegion"
    if ctr.is_file():
        labels = _read_label_io_list(ctr)
        if labels.size != n_cells:
            raise ValueError(f"cellToRegion size {labels.size} != nCells {n_cells}")
        return labels.astype(np.int32), _region_names_from_properties(case_dir)

    zones = parse_cell_zones(poly_dir / "cellZones")
    if not zones:
        raise ValueError("cellZones empty or unreadable")
    cell_region = np.full(n_cells, -1, dtype=np.int32)
    names = [z.name for z in zones]
    for ri, z in enumerate(zones):
        for c in z.cell_labels:
            if 0 <= c < n_cells:
                cell_region[c] = ri
    if np.any(cell_region < 0):
        raise ValueError("Some cells are not in any cellZone")
    return cell_region, names


def _face_patch_map(patches: list[PatchInfo], n_faces: int) -> np.ndarray:
    out = np.full(n_faces, -1, dtype=np.int32)
    for pi, p in enumerate(patches):
        for fi in range(p.start_face, min(p.start_face + p.n_faces, n_faces)):
            out[fi] = pi
    return out


def _region_cell_zones(
    orig_zones: list[CellZoneInfo],
    region_id: int,
    cell_region: np.ndarray,
    cell_map: dict[int, int],
) -> list[CellZoneInfo]:
    out: list[CellZoneInfo] = []
    for z in orig_zones:
        labels = sorted(
            cell_map[int(c)]
            for c in z.cell_labels
            if 0 <= int(c) < cell_region.size
            and int(cell_region[int(c)]) == region_id
            and int(c) in cell_map
        )
        if labels:
            out.append(CellZoneInfo(name=z.name, cell_labels=labels))
    return out


def _extract_region_mesh(
    region_id: int,
    region_name: str,
    region_names: list[str],
    points: np.ndarray,
    offsets: np.ndarray,
    conn: np.ndarray,
    owner: np.ndarray,
    nb: np.ndarray,
    patches: list[PatchInfo],
    face_patch: np.ndarray,
    cell_region: np.ndarray,
    orig_cell_zones: list[CellZoneInfo],
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    list[PatchInfo],
    int,
    list[CellZoneInfo],
]:
    n_faces = owner.size
    cell_mask = cell_region == region_id
    cells = np.where(cell_mask)[0]
    cell_set = set(cells.tolist())

    internal: list[tuple[np.ndarray, int, int]] = []
    boundary: dict[str, list[tuple[np.ndarray, int]]] = defaultdict(list)
    coupling: dict[str, list[tuple[np.ndarray, int]]] = defaultdict(list)

    for fi in range(n_faces):
        o = int(owner[fi])
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        verts = conn[s:e].copy()
        n = int(nb[fi])

        if o in cell_set:
            if n >= 0:
                if n in cell_set:
                    own, nei = (o, n) if o < n else (n, o)
                    if o != own:
                        verts = verts[::-1].copy()
                    internal.append((verts, own, nei))
                else:
                    other = region_names[int(cell_region[n])]
                    coupling[f"{region_name}_to_{other}"].append((verts, o))
            else:
                pi = int(face_patch[fi])
                pname = patches[pi].name if pi >= 0 else "unknown"
                boundary[pname].append((verts, o))
        elif n >= 0 and n in cell_set:
            verts = verts[::-1].copy()
            if n in cell_set and o >= 0 and o not in cell_set:
                other = region_names[int(cell_region[o])]
                coupling[f"{region_name}_to_{other}"].append((verts, n))

    internal.sort(key=lambda x: (x[1], x[2]))
    n_internal = len(internal)

    ordered: list[tuple[np.ndarray, int, int | None]] = [
        (v, own, nei) for v, own, nei in internal
    ]
    final_patches: list[PatchInfo] = []
    cursor = n_internal

    for p in patches:
        faces = boundary.get(p.name, [])
        if not faces:
            continue
        final_patches.append(
            PatchInfo(
                name=p.name,
                patch_type=p.patch_type,
                n_faces=len(faces),
                start_face=cursor,
                sample_mode=p.sample_mode,
                sample_region=p.sample_region,
                sample_patch=p.sample_patch,
                neighbour_patch=p.neighbour_patch,
                rotation_axis=p.rotation_axis,
                match_tolerance=p.match_tolerance,
                transform=p.transform,
            )
        )
        for v, o in faces:
            ordered.append((v, o, None))
        cursor += len(faces)

    for pname in sorted(coupling.keys()):
        faces = coupling[pname]
        remote = pname[len(region_name) + 4 :]  # strip "{region}_to_"
        final_patches.append(
            mapped_wall_patch(
                region_name,
                remote,
                n_faces=len(faces),
                start_face=cursor,
            )
        )
        for v, o in faces:
            ordered.append((v, o, None))
        cursor += len(faces)

    used_pts = sorted({int(v) for v, _, _ in ordered for v in v})
    pt_map = {old: new for new, old in enumerate(used_pts)}
    new_points = points[np.asarray(used_pts, dtype=np.int32)]
    cell_map = {old: new for new, old in enumerate(sorted(cells))}

    n_out = len(ordered)
    new_offsets = np.zeros(n_out + 1, dtype=np.int32)
    new_conn_parts: list[np.ndarray] = []
    new_owner = np.empty(n_out, dtype=np.int32)
    new_nb = np.full(n_out, -1, dtype=np.int32)
    pos = 0

    for fi, (verts, own, nei) in enumerate(ordered):
        mapped = np.asarray([pt_map[int(v)] for v in verts], dtype=np.int32)
        new_offsets[fi] = pos
        new_conn_parts.append(mapped)
        pos += mapped.size
        new_owner[fi] = cell_map[own]
        if nei is not None:
            new_nb[fi] = cell_map[nei]

    new_offsets[n_out] = pos
    new_conn = np.concatenate(new_conn_parts) if new_conn_parts else np.empty(0, dtype=np.int32)
    region_zones = _region_cell_zones(orig_cell_zones, region_id, cell_region, cell_map)
    return new_points, new_owner, new_nb, new_offsets, new_conn, final_patches, n_internal, region_zones


def _write_region_poly(
    poly_dir: Path,
    points: np.ndarray,
    owner: np.ndarray,
    nb: np.ndarray,
    offsets: np.ndarray,
    conn: np.ndarray,
    patches: list[PatchInfo],
    n_internal: int,
    cell_zones: list[CellZoneInfo] | None = None,
) -> None:
    poly_dir.mkdir(parents=True, exist_ok=True)
    _write_binary_vector_field(poly_dir / "points", points, _default_header("vectorField", "points"))
    _write_binary_compact_face_list(
        poly_dir / "faces", offsets, conn, _default_header("faceCompactList", "faces")
    )
    _write_binary_label_list(poly_dir / "owner", owner.astype(np.int32), _default_header("labelList", "owner"))
    _write_binary_label_list(
        poly_dir / "neighbour",
        nb[:n_internal].astype(np.int32),
        _default_header("labelList", "neighbour"),
    )
    write_boundary(poly_dir / "boundary", patches, _default_boundary_header())
    if cell_zones:
        write_cell_zones_v2412(poly_dir / "cellZones", cell_zones)


def interface_neighbors(case_dir: Path) -> dict[str, list[str]]:
    """Map each region name to coupled neighbour region names (from monolithic mesh)."""
    poly = case_dir / "constant" / "polyMesh"
    if not poly.is_dir():
        return {}
    owner = _read_binary_label_list(poly / "owner")
    nb_raw = _read_binary_label_list(poly / "neighbour")
    n_cells = int(owner.max()) + 1
    cell_region, names = _cell_region_map(case_dir, poly, n_cells)
    nb = np.full(owner.size, -1, dtype=np.int32)
    nb[: nb_raw.size] = nb_raw

    neighbors: dict[str, set[str]] = {n: set() for n in names}
    for fi in range(owner.size):
        n = int(nb[fi])
        if n < 0:
            continue
        o = int(owner[fi])
        ro, rn = int(cell_region[o]), int(cell_region[n])
        if ro != rn and ro >= 0 and rn >= 0:
            neighbors[names[ro]].add(names[rn])
            neighbors[names[rn]].add(names[ro])
    return {k: sorted(v) for k, v in neighbors.items()}


def field_patches_for_region(
    region_foam_name: str,
    *,
    config_name: str,
    monolithic_patch_names: list[str],
    patch_region: dict[str, str],
    neighbors: dict[str, list[str]],
) -> list[str]:
    """Patch names for 0/ fields after region split."""
    ext = [p for p in monolithic_patch_names if patch_region.get(p) == config_name]
    coupled = [f"{region_foam_name}_to_{nbr}" for nbr in neighbors.get(region_foam_name, [])]
    return ext + sorted(coupled)


def split_mesh_regions(case_dir: Path, region_names: list[str] | None = None) -> dict:
    """Write constant/<region>/polyMesh for each region; remove monolithic polyMesh."""
    poly = case_dir / "constant" / "polyMesh"
    points = _read_binary_vector_field(poly / "points")
    offsets, conn = _read_faces(poly / "faces")
    owner = _read_binary_label_list(poly / "owner")
    nb_raw = _read_binary_label_list(poly / "neighbour")
    patches = parse_boundary(poly / "boundary")
    n_faces = owner.size
    nb = np.full(n_faces, -1, dtype=np.int32)
    nb[: nb_raw.size] = nb_raw
    face_patch = _face_patch_map(patches, n_faces)

    n_cells = int(owner.max()) + 1
    cell_region, names = _cell_region_map(case_dir, poly, n_cells)
    orig_cell_zones = parse_cell_zones(poly / "cellZones")
    if region_names:
        names = list(region_names)

    report: dict = {"regions": {}, "method": "python_split"}
    for ri, rname in enumerate(names):
        mesh = _extract_region_mesh(
            ri,
            rname,
            names,
            points,
            offsets,
            conn,
            owner,
            nb,
            patches,
            face_patch,
            cell_region,
            orig_cell_zones,
        )
        out_poly = case_dir / "constant" / rname / "polyMesh"
        if out_poly.exists():
            shutil.rmtree(out_poly)
        _write_region_poly(out_poly, *mesh[:7], mesh[7])
        report["regions"][rname] = {
            "cells": int(np.sum(cell_region == ri)),
            "faces": int(mesh[1].size),
            "internal_faces": mesh[6],
            "patches": len(mesh[5]),
            "cell_zones": len(mesh[7]),
        }

    shutil.rmtree(poly)
    return report
